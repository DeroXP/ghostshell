//! What the exe carries of its backend: "the pack".
//!
//! The backend is a folder of files (the app, built as a folder by
//! backend.spec).  The exe carries that folder as one piece of bytes: a
//! list of the files, each with its length and the mark of what it holds
//! (SHA-256), and then the files themselves, each squeezed by itself.
//!
//!     8 bytes   "VSPPACK1"
//!     4 bytes   how long the list is (little end first)
//!     the list  JSON: the app's version, which file is the program to
//!               start, and for every file its path, its length, how many
//!               bytes it takes here, how it is squeezed, and its mark
//!     the files in the order of the list
//!
//! Three things are done with it: it is made (when the exe is built),
//! unpacked into a folder, and held against a folder that is there already.
//! A file that does not come out with its length and its mark is an error,
//! whatever the reason: nothing of the backend is ever run that is not what
//! the exe carries.
//!
//! Squeezing is Windows' own (win.rs): no code for it here, and no crate.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;

use crate::json::Json;
use crate::win;

pub const MAGIC: &[u8; 8] = b"VSPPACK1";

/// How a file is kept in the pack.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Way {
    /// As it is (nothing is gained by squeezing it, or it is empty).
    Plain,
    /// The same squeezing as a .zip file's.
    Mszip,
    /// Quick to bring back, not as small.
    XpressHuff,
    /// The smallest; slower to bring back.
    Lzms,
}

impl Way {
    pub const ALL: [Way; 4] = [Way::Plain, Way::Mszip, Way::XpressHuff, Way::Lzms];

    pub fn name(self) -> &'static str {
        match self {
            Way::Plain => "plain",
            Way::Mszip => "mszip",
            Way::XpressHuff => "xpress-huff",
            Way::Lzms => "lzms",
        }
    }

    pub fn named(name: &str) -> Option<Way> {
        Way::ALL.into_iter().find(|way| way.name() == name)
    }

    /// Windows' number for it.
    fn of_windows(self) -> Option<u32> {
        match self {
            Way::Plain => None,
            Way::Mszip => Some(2),
            Way::XpressHuff => Some(4),
            Way::Lzms => Some(5),
        }
    }
}

/// One file of the pack.
#[derive(Debug, Clone, PartialEq)]
pub struct Kept {
    /// Where it goes in the folder, with backslashes: `_internal\python312.dll`.
    pub path: String,
    pub size: usize,
    /// The SHA-256 of what it holds, in sixty-four letters.
    pub mark: String,
    way: Way,
    at: usize,
    takes: usize,
}

/// A pack, read.
#[derive(Debug)]
pub struct Pack<'a> {
    bytes: &'a [u8],
    /// The app's version it was made for.
    pub version: String,
    /// The program to start, as a path in the folder.
    pub start: String,
    pub files: Vec<Kept>,
    /// The mark of the whole pack: what tells one pack from every other.
    pub id: String,
}

/// A path as the pack may name one: inside the folder, and nowhere else.
/// With backslashes; None for anything that could lead out of the folder
/// or that Windows would read as something else.
fn inside(path: &str) -> Option<String> {
    let parts: Vec<&str> = path.split(['/', '\\']).collect();
    let fine = |part: &&str| {
        !part.is_empty() && *part != "." && *part != ".." && !part.ends_with([' ', '.'])
            && part.chars().all(|c| c >= ' ' && !"<>:\"|?*".contains(c))
    };
    (path.len() <= 200 && !parts.is_empty() && parts.iter().all(fine)).then(|| parts.join("\\"))
}

/// Do `work` for every one of `items`, on a few threads.  The first error.
fn for_each<T: Sync>(items: &[T], work: impl Fn(&T) -> Result<(), String> + Sync) -> Result<(), String> {
    let (next, failed) = (AtomicUsize::new(0), Mutex::new(None::<String>));
    let threads = std::thread::available_parallelism().map_or(2, |n| n.get()).clamp(1, 6).min(items.len().max(1));
    std::thread::scope(|scope| {
        for _ in 0..threads {
            scope.spawn(|| loop {
                let at = next.fetch_add(1, Ordering::Relaxed);
                if at >= items.len() || failed.lock().unwrap_or_else(|p| p.into_inner()).is_some() {
                    return;
                }
                if let Err(why) = work(&items[at]) {
                    failed.lock().unwrap_or_else(|p| p.into_inner()).get_or_insert(why);
                }
            });
        }
    });
    match failed.into_inner().unwrap_or_else(|p| p.into_inner()) {
        Some(why) => Err(why),
        None => Ok(()),
    }
}

/// Everything in `folder`: its files (path inside the folder, with
/// backslashes, and length) and its folders.  A link anywhere in it is an
/// error: what a link points to is not in the folder.
fn listing(folder: &str) -> Result<(Vec<(String, u64)>, Vec<String>), String> {
    use std::os::windows::fs::MetadataExt as _;
    let (mut files, mut folders, mut to_read) = (Vec::new(), Vec::new(), vec![String::new()]);
    while let Some(inside_it) = to_read.pop() {
        let here: PathBuf = if inside_it.is_empty() { PathBuf::from(folder) } else { Path::new(folder).join(&inside_it) };
        for entry in std::fs::read_dir(&here).map_err(|e| format!("{}: {e}", here.display()))? {
            let entry = entry.map_err(|e| format!("{}: {e}", here.display()))?;
            let name = entry.file_name().to_string_lossy().into_owned();
            let path = if inside_it.is_empty() { name } else { format!("{inside_it}\\{name}") };
            let found = entry.metadata().map_err(|e| format!("{path}: {e}"))?;
            if found.file_attributes() & 0x400 != 0 {
                return Err(format!("{path} is a link"));
            }
            if found.is_dir() {
                folders.push(path.clone());
                to_read.push(path);
            } else {
                files.push((path, found.len()));
            }
        }
    }
    files.sort();
    folders.sort();
    Ok((files, folders))
}

/// Make a pack of everything in `folder`.  `start` is the program to start,
/// as a path in the folder.  A file is squeezed `way` where that makes it
/// smaller, and kept as it is otherwise.
pub fn make(folder: &str, version: &str, start: &str, way: Way) -> Result<Vec<u8>, String> {
    let (files, _) = listing(folder)?;
    let start = inside(start).ok_or_else(|| format!("{start} is not a path inside the folder"))?;
    if !files.iter().any(|(path, _)| path.eq_ignore_ascii_case(&start)) {
        return Err(format!("{folder} has no {start} to start"));
    }
    // (each file by itself, so that they can be brought back side by side)
    let made: Mutex<Vec<Option<(String, usize, String, Way, Vec<u8>)>>> = Mutex::new(vec![None; files.len()]);
    let numbered: Vec<(usize, &(String, u64))> = files.iter().enumerate().collect();
    for_each(&numbered, |(at, (path, _))| {
        if inside(path).as_deref() != Some(path.as_str()) {
            return Err(format!("{path} cannot be carried under that name"));
        }
        let data = std::fs::read(Path::new(folder).join(path)).map_err(|e| format!("{path}: {e}"))?;
        let mark = win::sha256_hex(&data);
        let (kept_as, kept) = match way.of_windows() {
            Some(number) if !data.is_empty() => {
                let squeezed = win::squeeze(number, &data)?;
                if squeezed.len() < data.len() { (way, squeezed) } else { (Way::Plain, data.clone()) }
            }
            _ => (Way::Plain, data.clone()),
        };
        made.lock().unwrap_or_else(|p| p.into_inner())[*at] = Some((path.clone(), data.len(), mark, kept_as, kept));
        Ok(())
    })?;
    let made = made.into_inner().unwrap_or_else(|p| p.into_inner());
    let (mut list, mut data) = (Vec::new(), Vec::new());
    for (path, size, mark, kept_as, kept) in made.into_iter().flatten() {
        list.push(Json::object([
            ("path", path.as_str().into()),
            ("size", (size as u64).into()),
            ("takes", (kept.len() as u64).into()),
            ("way", kept_as.name().into()),
            ("sha256", mark.as_str().into()),
        ]));
        data.extend_from_slice(&kept);
    }
    let header = Json::object([("version", version.into()), ("start", start.as_str().into()), ("files", Json::Array(list))]).to_string();
    let mut pack = Vec::with_capacity(12 + header.len() + data.len());
    pack.extend_from_slice(MAGIC);
    pack.extend_from_slice(&(header.len() as u32).to_le_bytes());
    pack.extend_from_slice(header.as_bytes());
    pack.extend_from_slice(&data);
    Ok(pack)
}

/// Can an exe of the app's version `version` carry what `bytes` holds?
/// What it holds, in a line, or why not.
///
/// The pack is read whole: its list, and every file taken out of it once,
/// each with its length and its mark.  And it has to have been made for
/// this version of the app.  Asked when the exe is built
/// (shell_rs/app/build.rs), so that a pack that is damaged, or the pack of
/// another version, stops the build, and is not found on a person's PC,
/// where the app would only say that it cannot get its background program
/// ready.
pub fn fit_to_carry(bytes: &[u8], version: &str) -> Result<String, String> {
    let pack = Pack::read(bytes)?;
    if pack.version != version {
        return Err(format!("it was made for version {} of the app, and this is version {version}", pack.version));
    }
    for_each(&pack.files, |file| pack.bytes_of(file).map(drop))?;
    Ok(format!("{} files, {} bytes unpacked, {} bytes carried, for version {}", pack.files.len(), pack.size(), bytes.len(), pack.version))
}

impl<'a> Pack<'a> {
    /// Read a pack.  Everything it says is checked here, before anything is
    /// done with it: a pack that names a path outside the folder, or two
    /// files under one name, or more bytes than it has, is not a pack.
    pub fn read(bytes: &'a [u8]) -> Result<Pack<'a>, String> {
        let not_one = |why: &str| format!("what the exe carries of its background program cannot be read ({why})");
        if bytes.len() < 12 || &bytes[..8] != MAGIC {
            return Err(not_one("it does not begin as a pack begins"));
        }
        let listed = u32::from_le_bytes([bytes[8], bytes[9], bytes[10], bytes[11]]) as usize;
        let data_at = 12usize.checked_add(listed).filter(|end| *end <= bytes.len()).ok_or_else(|| not_one("its list is cut short"))?;
        let header = std::str::from_utf8(&bytes[12..data_at]).ok().and_then(|text| Json::parse(text).ok()).ok_or_else(|| not_one("its list cannot be read"))?;
        let version = header["version"].as_str().filter(|v| !v.is_empty()).ok_or_else(|| not_one("it names no version"))?.to_string();
        let start = header["start"].as_str().and_then(inside).ok_or_else(|| not_one("it names no program to start"))?;
        let (mut files, mut at, mut names) = (Vec::new(), data_at, BTreeSet::new());
        for file in header["files"].as_array().ok_or_else(|| not_one("it lists no files"))? {
            let path = file["path"].as_str().and_then(inside).ok_or_else(|| not_one("it names a path outside its folder"))?;
            let whole = |name: &str| file[name].as_int().and_then(|n| usize::try_from(n).ok());
            let (size, takes) = (whole("size").ok_or_else(|| not_one("a length is missing"))?, whole("takes").ok_or_else(|| not_one("a length is missing"))?);
            let way = file["way"].as_str().and_then(Way::named).ok_or_else(|| not_one("a file is squeezed in a way this program does not know"))?;
            let mark = file["sha256"].as_str().filter(|m| m.len() == 64 && m.bytes().all(|b| b.is_ascii_hexdigit())).ok_or_else(|| not_one("a file has no mark"))?;
            let end = at.checked_add(takes).filter(|end| *end <= bytes.len()).ok_or_else(|| not_one("it lists more than it has"))?;
            if (way == Way::Plain && takes != size) || !names.insert(path.to_lowercase()) {
                return Err(not_one("its list contradicts itself"));
            }
            files.push(Kept { path, size, mark: mark.to_lowercase(), way, at, takes });
            at = end;
        }
        if at != bytes.len() || !files.iter().any(|file| file.path.eq_ignore_ascii_case(&start)) {
            return Err(not_one("its list and what it has do not fit together"));
        }
        Ok(Pack { bytes, version, start, files, id: win::sha256_hex(bytes) })
    }

    /// How many bytes the files are, unpacked.
    pub fn size(&self) -> usize {
        self.files.iter().map(|file| file.size).sum()
    }

    /// What one file holds: of its length, and with its mark, or an error.
    pub fn bytes_of(&self, file: &Kept) -> Result<Vec<u8>, String> {
        let kept = &self.bytes[file.at..file.at + file.takes];
        let data = match file.way.of_windows() {
            Some(number) => win::unsqueeze(number, kept, file.size).map_err(|why| format!("{}: {why}", file.path))?,
            None => kept.to_vec(),
        };
        if data.len() != file.size || win::sha256_hex(&data) != file.mark {
            return Err(format!("{} did not come out of the exe as it went in", file.path));
        }
        Ok(data)
    }

    /// Unpack everything into `folder`, which is there and empty (or holds
    /// only what the caller put there).  On an error the folder is the
    /// caller's to take away: what is in it is not to be used.
    pub fn unpack_into(&self, folder: &str) -> Result<(), String> {
        // The folders first, one after the other; then the files, side by side.
        let folders: BTreeSet<&str> = self.files.iter().filter_map(|file| file.path.rsplit_once('\\').map(|(above, _)| above)).collect();
        for inner in folders {
            std::fs::create_dir_all(Path::new(folder).join(inner)).map_err(|e| format!("{inner}: {e}"))?;
        }
        for_each(&self.files, |file| {
            let data = self.bytes_of(file)?;
            std::fs::write(Path::new(folder).join(&file.path), data).map_err(|e| format!("{}: {e}", file.path))
        })
    }

    /// Is `folder` exactly what this pack unpacks to?  The same files under
    /// the same names, each with its length and its mark; the same folders;
    /// nothing else in it (`but` names what the caller put there itself);
    /// no link anywhere.  Why not, if not.
    pub fn same_as(&self, folder: &str, but: &[&str]) -> Result<(), String> {
        let (found, folders) = listing(folder)?;
        let found: Vec<(String, u64)> = found.into_iter().filter(|(path, _)| !but.iter().any(|own| path.eq_ignore_ascii_case(own))).collect();
        let lower = |path: &str| path.to_lowercase();
        let there: BTreeSet<String> = found.iter().map(|(path, _)| lower(path)).collect();
        let wanted: BTreeSet<String> = self.files.iter().map(|file| lower(&file.path)).collect();
        if let Some(extra) = there.difference(&wanted).next() {
            return Err(format!("{extra} is in the folder and is not the exe's"));
        }
        if let Some(missing) = wanted.difference(&there).next() {
            return Err(format!("{missing} is missing from the folder"));
        }
        let wanted_folders: BTreeSet<String> = self
            .files
            .iter()
            .flat_map(|file| {
                let parts: Vec<&str> = file.path.split('\\').collect();
                (1..parts.len()).map(|n| lower(&parts[..n].join("\\"))).collect::<Vec<_>>()
            })
            .collect();
        if let Some(extra) = folders.iter().map(|path| lower(path)).find(|path| !wanted_folders.contains(path)) {
            return Err(format!("the folder {extra} is not the exe's"));
        }
        let sizes: std::collections::BTreeMap<String, u64> = found.iter().map(|(path, size)| (lower(path), *size)).collect();
        for_each(&self.files, |file| {
            if sizes.get(&lower(&file.path)) != Some(&(file.size as u64)) {
                return Err(format!("{} is not as long as the exe's", file.path));
            }
            let data = std::fs::read(Path::new(folder).join(&file.path)).map_err(|e| format!("{}: {e}", file.path))?;
            if data.len() != file.size || win::sha256_hex(&data) != file.mark {
                return Err(format!("{} is not what the exe carries", file.path));
            }
            Ok(())
        })
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    pub(crate) fn a_place(name: &str) -> String {
        let place = std::env::temp_dir().join(format!("vispora-shell-test-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&place);
        std::fs::create_dir_all(&place).unwrap();
        place.to_string_lossy().into_owned()
    }

    /// A small folder that stands for a backend: a program to start, files
    /// beside it and below it, one of them empty, one that does not squeeze.
    pub(crate) fn a_backend(place: &str) -> String {
        let folder = format!("{place}\\VisporaBackend");
        std::fs::create_dir_all(format!("{folder}\\_internal\\certifi")).unwrap();
        std::fs::write(format!("{folder}\\VisporaBackend.exe"), b"MZ not really a program").unwrap();
        std::fs::write(format!("{folder}\\_internal\\python312.dll"), "a library, a library, a library. ".repeat(4000)).unwrap();
        std::fs::write(format!("{folder}\\_internal\\certifi\\cacert.pem"), "-----BEGIN CERTIFICATE-----\n".repeat(900)).unwrap();
        std::fs::write(format!("{folder}\\_internal\\empty.txt"), b"").unwrap();
        let mut noise = vec![0u8; 5000];
        crate::platform::Platform::random(&*win::platform(), &mut noise).unwrap();
        std::fs::write(format!("{folder}\\_internal\\noise.bin"), noise).unwrap();
        folder
    }

    #[test]
    fn a_folder_is_packed_and_comes_out_as_it_went_in() {
        let place = a_place("pack");
        let folder = a_backend(&place);
        for way in Way::ALL {
            let bytes = make(&folder, "3.8.1", "VisporaBackend.exe", way).unwrap();
            let pack = Pack::read(&bytes).unwrap();
            assert_eq!((pack.version.as_str(), pack.start.as_str(), pack.files.len()), ("3.8.1", "VisporaBackend.exe", 5));
            assert_eq!(pack.size(), 23 + 132_000 + 25_200 + 5000);
            assert_eq!(pack.id, win::sha256_hex(&bytes));
            let paths: Vec<&str> = pack.files.iter().map(|file| file.path.as_str()).collect();
            assert_eq!(paths, ["VisporaBackend.exe", "_internal\\certifi\\cacert.pem", "_internal\\empty.txt", "_internal\\noise.bin", "_internal\\python312.dll"]);
            // What does not get smaller is kept as it is; so is nothing at all.
            let kept = |name: &str| pack.files.iter().find(|file| file.path.ends_with(name)).unwrap().way;
            assert_eq!((kept("empty.txt"), kept("noise.bin"), kept("python312.dll")), (Way::Plain, Way::Plain, way));
            assert!(way == Way::Plain || bytes.len() < pack.size() / 5, "{}: {} bytes", way.name(), bytes.len());
            let out = format!("{place}\\out-{}", way.name());
            std::fs::create_dir(&out).unwrap();
            pack.unpack_into(&out).unwrap();
            assert_eq!(pack.same_as(&out, &[]), Ok(()));
            assert_eq!(pack.same_as(&folder, &[]), Ok(()), "the folder it was made of");
            assert_eq!(std::fs::read(format!("{out}\\_internal\\python312.dll")).unwrap(), std::fs::read(format!("{folder}\\_internal\\python312.dll")).unwrap());
        }
        // The same folder gives the same pack, and so the same name for it.
        assert_eq!(make(&folder, "3.8.1", "VisporaBackend.exe", Way::Lzms).unwrap(), make(&folder, "3.8.1", "VisporaBackend.exe", Way::Lzms).unwrap());
        assert!(make(&folder, "3.8.1", "Other.exe", Way::Lzms).is_err(), "a program to start that is not there");
        let _ = std::fs::remove_dir_all(&place);
    }

    #[test]
    fn a_folder_that_is_not_what_the_exe_carries_is_told_apart() {
        let place = a_place("held-against");
        let folder = a_backend(&place);
        let bytes = make(&folder, "3.8.1", "VisporaBackend.exe", Way::XpressHuff).unwrap();
        let pack = Pack::read(&bytes).unwrap();
        let fresh = |name: &str| {
            let out = format!("{place}\\{name}");
            std::fs::create_dir(&out).unwrap();
            pack.unpack_into(&out).unwrap();
            out
        };
        // One byte of a file is another byte (same length).
        let out = fresh("changed");
        let mut library = std::fs::read(format!("{out}\\_internal\\python312.dll")).unwrap();
        library[70_000] ^= 1;
        std::fs::write(format!("{out}\\_internal\\python312.dll"), library).unwrap();
        assert_eq!(pack.same_as(&out, &[]).unwrap_err(), "_internal\\python312.dll is not what the exe carries");
        // A file is longer, a file is missing, a file too many, a folder too many.
        let out = fresh("longer");
        std::fs::write(format!("{out}\\VisporaBackend.exe"), b"MZ not really a program, and more").unwrap();
        assert_eq!(pack.same_as(&out, &[]).unwrap_err(), "VisporaBackend.exe is not as long as the exe's");
        let out = fresh("missing");
        std::fs::remove_file(format!("{out}\\_internal\\empty.txt")).unwrap();
        assert_eq!(pack.same_as(&out, &[]).unwrap_err(), "_internal\\empty.txt is missing from the folder");
        let out = fresh("one-more");
        std::fs::write(format!("{out}\\_internal\\sitecustomize.py"), b"import os").unwrap();
        assert_eq!(pack.same_as(&out, &[]).unwrap_err(), "_internal\\sitecustomize.py is in the folder and is not the exe's");
        // (what the caller put there itself is named, and only that passes)
        assert_eq!(pack.same_as(&out, &["_internal\\sitecustomize.py"]), Ok(()));
        let out = fresh("a-folder-more");
        std::fs::create_dir(format!("{out}\\_internal\\plugins")).unwrap();
        assert_eq!(pack.same_as(&out, &[]).unwrap_err(), "the folder _internal\\plugins is not the exe's");
        // A link in it, to a folder that holds the right files.
        let out = fresh("a-link");
        std::fs::remove_dir_all(format!("{out}\\_internal\\certifi")).unwrap();
        let linked = std::process::Command::new("cmd.exe")
            .args(["/d", "/c", "mklink", "/J", &format!("{out}\\_internal\\certifi"), &format!("{folder}\\_internal\\certifi")])
            .output()
            .unwrap();
        assert!(linked.status.success());
        assert_eq!(pack.same_as(&out, &[]).unwrap_err(), "_internal\\certifi is a link");
        let _ = std::fs::remove_dir(format!("{out}\\_internal\\certifi"));
        let _ = std::fs::remove_dir_all(&place);
    }

    #[test]
    fn what_is_not_a_pack_is_not_read_as_one() {
        let place = a_place("not-a-pack");
        let folder = a_backend(&place);
        let good = make(&folder, "3.8.1", "VisporaBackend.exe", Way::Mszip).unwrap();
        assert!(Pack::read(&good).is_ok());
        for cut in [0, 7, 11, 12, 40, good.len() - 1] {
            assert!(Pack::read(&good[..cut]).is_err(), "cut at {cut}");
        }
        let mut longer = good.clone();
        longer.push(0);
        assert!(Pack::read(&longer).is_err(), "a byte too many");
        let mut other = good.clone();
        other[0] = b'X';
        assert!(Pack::read(&other).is_err());
        // A list of its own making: paths that lead out of the folder, two
        // files under one name, a program to start that is not listed.
        let with_list = |list: &str, data: &[u8]| {
            let mut pack = MAGIC.to_vec();
            pack.extend_from_slice(&(list.len() as u32).to_le_bytes());
            pack.extend_from_slice(list.as_bytes());
            pack.extend_from_slice(data);
            pack
        };
        let mark = win::sha256_hex(b"x");
        let file = |path: &str| format!(r#"{{"path": "{path}", "size": 1, "takes": 1, "way": "plain", "sha256": "{mark}"}}"#);
        let fine = with_list(&format!(r#"{{"version": "1", "start": "a.exe", "files": [{}]}}"#, file("a.exe")), b"x");
        assert_eq!(Pack::read(&fine).unwrap().files[0].path, "a.exe");
        for path in ["..\\\\a.exe", "x/../../a.exe", "C:\\\\a.exe", "\\\\\\\\server\\\\share\\\\a.exe", "/a.exe", "a.exe ", "a.exe.", "", "a\\\\\\\\b.exe", "x\\\\con:stream"] {
            let bad = with_list(&format!(r#"{{"version": "1", "start": "a.exe", "files": [{}, {}]}}"#, file("a.exe"), file(path)), b"xx");
            assert!(Pack::read(&bad).is_err(), "{path}");
        }
        let twice = with_list(&format!(r#"{{"version": "1", "start": "a.exe", "files": [{}, {}]}}"#, file("a.exe"), file("A.EXE")), b"xx");
        assert!(Pack::read(&twice).is_err(), "two files under one name");
        let none = with_list(&format!(r#"{{"version": "1", "start": "b.exe", "files": [{}]}}"#, file("a.exe")), b"x");
        assert!(Pack::read(&none).is_err(), "a program to start that is not in the list");
        let short = with_list(&format!(r#"{{"version": "1", "start": "a.exe", "files": [{}, {}]}}"#, file("a.exe"), file("b.exe")), b"x");
        assert!(Pack::read(&short).is_err(), "more listed than there is");
        // What it holds is not what its list says: found when it is taken out.
        let mut spoiled = good.clone();
        let last = spoiled.len() - 3;
        spoiled[last] ^= 0x55;
        let pack = Pack::read(&spoiled).unwrap();
        let out = format!("{place}\\spoiled");
        std::fs::create_dir(&out).unwrap();
        assert!(pack.unpack_into(&out).is_err(), "bytes that are not the listed file's were written out");
        // The same for a file that is kept as it is, where nothing but its
        // mark can tell: one byte is another, and the length is right.
        let fine = Pack::read(&fine).unwrap();
        assert_eq!(fine.bytes_of(&fine.files[0]).unwrap(), b"x");
        let other_byte = with_list(&format!(r#"{{"version": "1", "start": "a.exe", "files": [{}]}}"#, file("a.exe")), b"y");
        let pack = Pack::read(&other_byte).unwrap();
        assert_eq!(pack.bytes_of(&pack.files[0]).unwrap_err(), "a.exe did not come out of the exe as it went in");
        let out = format!("{place}\\other-byte");
        std::fs::create_dir(&out).unwrap();
        assert!(pack.unpack_into(&out).is_err() && !Path::new(&format!("{out}\\a.exe")).exists(), "it was written out");

        // What an exe may carry is asked when it is built: a pack that is
        // whole, and made for the version the exe is built for.
        assert_eq!(fit_to_carry(&good, "3.8.1").unwrap(), format!("5 files, 162223 bytes unpacked, {} bytes carried, for version 3.8.1", good.len()));
        assert_eq!(fit_to_carry(&good, "3.8.2").unwrap_err(), "it was made for version 3.8.1 of the app, and this is version 3.8.2");
        for (what, damaged) in [("cut short", &good[..good.len() - 1]), ("a byte of a squeezed file is another", &spoiled[..]), ("not a pack", &b"VSPPACK1 and no more"[..])] {
            assert!(fit_to_carry(damaged, "3.8.1").is_err(), "{what}");
        }
        // (one that reads as a pack and whose list is right: only taking
        // every file out tells that a file kept as it is holds another byte)
        assert!(Pack::read(&other_byte).is_ok());
        assert_eq!(fit_to_carry(&other_byte, "1").unwrap_err(), "a.exe did not come out of the exe as it went in");
        let _ = std::fs::remove_dir_all(&place);
    }
}
