//! The network cards' byte counts, for the network monitor's totals.
//!
//! `q: network_cards` - `[{"name", "sent", "recv"}]`, one row for every
//! network adapter Windows lists, in its order: the adapter's friendly name
//! ("Ethernet", "Wi-Fi") and the bytes it has sent and received since it
//! came up.  These are the calls psutil's `net_io_counters()` makes, which is
//! what the app read them with: `GetAdaptersAddresses` for the list, then
//! `GetIfEntry2` for each adapter by its index; the list is asked for
//! without the addresses and DNS details psutil has Windows add to it,
//! which nothing here uses and which cost Windows' DNS Client service
//! work at every call (`ONLY_THE_ADAPTERS`).  As in psutil, the question
//! fails as a whole where Windows will not list the adapters, or will not
//! say the counts of one of them.  (Two adapters of one name, and counts
//! that start again from nought, are the caller's to deal with, as psutil
//! deals with them in Python: the rows are as Windows gives them.)
//!
//! It only reads.

use std::ffi::c_void;

use crate::json::Json;
use crate::win32::read;

/// GetAdaptersAddresses: every address family (psutil's call), and none of
/// the addresses and DNS details it would add to each adapter, which are
/// not used here.  psutil asks with no flags, and then Windows asks its DNS
/// Client service for every adapter's DNS servers and suffix at every
/// call: measured on this PC, that service used about 15% of a core while
/// the list was asked for fifty times a second (a third of a percent at the
/// monitor's once a second), and nothing with these flags.  The adapters,
/// their order, names and indexes do not depend on them (tests hold that
/// against psutil's own list).
const AF_UNSPEC: u32 = 0;
const GAA_FLAG_SKIP_UNICAST: u32 = 0x1;
const GAA_FLAG_SKIP_ANYCAST: u32 = 0x2;
const GAA_FLAG_SKIP_MULTICAST: u32 = 0x4;
const GAA_FLAG_SKIP_DNS_SERVER: u32 = 0x8;
const GAA_FLAG_SKIP_DNS_INFO: u32 = 0x800;
pub(crate) const ONLY_THE_ADAPTERS: u32 = GAA_FLAG_SKIP_UNICAST | GAA_FLAG_SKIP_ANYCAST | GAA_FLAG_SKIP_MULTICAST | GAA_FLAG_SKIP_DNS_SERVER | GAA_FLAG_SKIP_DNS_INFO;
const ERROR_BUFFER_OVERFLOW: u32 = 111;
const ERROR_SUCCESS: u32 = 0;

/// IP_ADAPTER_ADDRESSES (64-bit): the adapter's index, the next adapter,
/// its friendly name.
const ADAPTER_IF_INDEX: usize = 4;
const ADAPTER_NEXT: usize = 8;
const ADAPTER_FRIENDLY_NAME: usize = 72;

/// MIB_IF_ROW2: its size, the index it is asked by, its alias (the same
/// friendly name, for the tests) and the two counts.
pub(crate) const ROW_SIZE: usize = 1352;
const ROW_INDEX: usize = 8;
const ROW_IN_OCTETS: usize = 1208;
const ROW_OUT_OCTETS: usize = 1280;

#[link(name = "iphlpapi")]
extern "system" {
    fn GetAdaptersAddresses(family: u32, flags: u32, reserved: *mut c_void, addresses: *mut c_void, size: *mut u32) -> u32;
    fn GetIfEntry2(row: *mut c_void) -> u32;
}

/// One adapter: its friendly name, its index, and its counts.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Card {
    pub(crate) name: String,
    pub(crate) index: u32,
    pub(crate) sent: u64,
    pub(crate) recv: u64,
}

/// The two counts out of a MIB_IF_ROW2 as Windows filled it: (sent, received).
pub(crate) fn counts_of(row: &[u8]) -> Option<(u64, u64)> {
    Some((read(row, ROW_OUT_OCTETS)?, read(row, ROW_IN_OCTETS)?))
}

/// A text of UTF-16 units that ends with a nought, from a pointer Windows
/// gave.
///
/// # Safety
/// `text` must be null or point at such a text.
unsafe fn wide_text(text: *const u16) -> String {
    if text.is_null() {
        return String::new();
    }
    let mut length = 0;
    while *text.add(length) != 0 {
        length += 1;
    }
    String::from_utf16_lossy(std::slice::from_raw_parts(text, length))
}

/// The adapters Windows lists, asked with `flags`: (index, friendly name),
/// in its order.
pub(crate) fn adapters(flags: u32) -> Result<Vec<(u32, String)>, String> {
    // Asked twice, as psutil asks: once for the size, once for the list.
    let mut size: u32 = 0;
    let first = unsafe { GetAdaptersAddresses(AF_UNSPEC, flags, std::ptr::null_mut(), std::ptr::null_mut(), &mut size) };
    if first != ERROR_BUFFER_OVERFLOW {
        return Err(format!("GetAdaptersAddresses did not say how much room the list needs ({first})"));
    }
    let mut list = vec![0u64; size as usize / 8 + 1];
    let second = unsafe { GetAdaptersAddresses(AF_UNSPEC, flags, std::ptr::null_mut(), list.as_mut_ptr().cast(), &mut size) };
    if second != ERROR_SUCCESS {
        return Err(format!("GetAdaptersAddresses failed ({second})"));
    }
    let mut adapters = Vec::new();
    let mut at = list.as_ptr() as *const u8;
    while !at.is_null() {
        // SAFETY: `at` is the first adapter of the list Windows wrote into
        // `list`, or the `Next` of one: each is a whole IP_ADAPTER_ADDRESSES,
        // which is more than the 80 bytes read here.
        let adapter = unsafe { std::slice::from_raw_parts(at, ADAPTER_FRIENDLY_NAME + 8) };
        let index: u32 = read(adapter, ADAPTER_IF_INDEX).unwrap_or(0);
        let next: usize = read(adapter, ADAPTER_NEXT).unwrap_or(0);
        let name_at: usize = read(adapter, ADAPTER_FRIENDLY_NAME).unwrap_or(0);
        adapters.push((index, unsafe { wide_text(name_at as *const u16) }));
        at = next as *const u8;
    }
    Ok(adapters)
}

/// The adapters, with their counts, in Windows' order.
pub(crate) fn cards() -> Result<Vec<Card>, String> {
    let mut cards = Vec::new();
    for (index, name) in adapters(ONLY_THE_ADAPTERS)? {
        let mut row = vec![0u64; ROW_SIZE / 8];
        let raw = unsafe { std::slice::from_raw_parts_mut(row.as_mut_ptr() as *mut u8, ROW_SIZE) };
        raw[ROW_INDEX..ROW_INDEX + 4].copy_from_slice(&index.to_le_bytes());
        let told = unsafe { GetIfEntry2(row.as_mut_ptr().cast()) };
        if told != ERROR_SUCCESS {
            return Err(format!("GetIfEntry2 failed for the adapter {name:?} ({told})"));
        }
        let raw = unsafe { std::slice::from_raw_parts(row.as_ptr() as *const u8, ROW_SIZE) };
        let (sent, recv) = counts_of(raw).ok_or("an adapter's row is too short")?;
        cards.push(Card { name, index, sent, recv });
    }
    Ok(cards)
}

/// `q: network_cards`.
pub fn network_cards() -> Result<Json, String> {
    let rows = cards()?
        .into_iter()
        .map(|card| Json::object([("name", card.name.into()), ("sent", card.sent.into()), ("recv", card.recv.into())]))
        .collect();
    Ok(Json::Array(rows))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[link(name = "iphlpapi")]
    extern "system" {
        fn GetIfEntry(row: *mut c_void) -> u32;
    }

    /// Where MIB_IF_ROW2 has the adapter's alias.
    const ROW_ALIAS: usize = 28;

    /// MIB_IFROW, the older row, laid out another way: its index at 512, and
    /// 32-bit counts at 552 (received) and 576 (sent).
    const OLD_ROW_SIZE: usize = 860;

    fn old_counts(index: u32) -> Option<(u32, u32)> {
        let mut row = vec![0u32; OLD_ROW_SIZE / 4];
        row[512 / 4] = index;
        if unsafe { GetIfEntry(row.as_mut_ptr().cast()) } != 0 {
            return None;
        }
        Some((row[576 / 4], row[552 / 4]))
    }

    #[test]
    fn the_counts_are_read_where_windows_writes_them() {
        let mut row = vec![0u8; ROW_SIZE];
        // (every 8 bytes a number of their own, so that a count read from
        // another place is not mistaken for the right one)
        for (at, chunk) in row.chunks_exact_mut(8).enumerate() {
            chunk.copy_from_slice(&(1_000_000 + at as u64).to_le_bytes());
        }
        assert_eq!(counts_of(&row), Some((1_000_000 + 1280 / 8, 1_000_000 + 1208 / 8)));
        assert_eq!(counts_of(&row[..1281]), None);
    }

    #[test]
    fn the_adapters_are_the_ones_psutils_call_lists_without_the_details_left_out() {
        // psutil asks with no flags; the question leaves the addresses and
        // DNS details out.  The list, its order, names and indexes are the same.
        let theirs = adapters(0).unwrap();
        assert!(!theirs.is_empty());
        assert_eq!(adapters(ONLY_THE_ADAPTERS).unwrap(), theirs);
        assert_ne!(ONLY_THE_ADAPTERS & 0x20, 0x20, "GAA_FLAG_SKIP_FRIENDLY_NAME would leave the names out");
    }

    #[test]
    fn this_pc_lists_its_adapters_with_their_names_and_counts() {
        let cards = cards().unwrap();
        assert!(!cards.is_empty());
        assert!(cards.iter().all(|card| !card.name.is_empty()), "{cards:?}");
        // The row Windows filled for an adapter is that adapter's: its alias
        // is the adapter's friendly name.
        for card in &cards {
            let mut row = vec![0u64; ROW_SIZE / 8];
            let raw = unsafe { std::slice::from_raw_parts_mut(row.as_mut_ptr() as *mut u8, ROW_SIZE) };
            raw[ROW_INDEX..ROW_INDEX + 4].copy_from_slice(&card.index.to_le_bytes());
            assert_eq!(unsafe { GetIfEntry2(row.as_mut_ptr().cast()) }, 0);
            let raw = unsafe { std::slice::from_raw_parts(row.as_ptr() as *const u8, ROW_SIZE) };
            let alias: Vec<u16> = raw[ROW_ALIAS..ROW_ALIAS + 514].chunks_exact(2).map(|pair| u16::from_le_bytes([pair[0], pair[1]])).take_while(|unit| *unit != 0).collect();
            assert_eq!(String::from_utf16_lossy(&alias), card.name, "the row of adapter {}", card.index);
        }
        // The counts are the ones Windows' older call says, which lays its
        // row out another way and counts in 32 bits: the same, but for what
        // went over the line in between, for every adapter that has
        // counted anything at all.
        let counted: Vec<&Card> = cards.iter().filter(|card| card.recv > 0 || card.sent > 0).collect();
        assert!(!counted.is_empty(), "no adapter has counted a byte: {cards:?}");
        for card in counted {
            let Some((sent, recv)) = old_counts(card.index) else { continue };
            let after = cards_now(card.index);
            let near = |old: u32, before: u64, later: u64| {
                let (before, later) = (before % (1 << 32), later % (1 << 32));
                let old = old as u64;
                (before <= old && old <= later) || (later < before && (old >= before || old <= later))
            };
            assert!(near(sent, card.sent, after.0) && near(recv, card.recv, after.1), "{card:?}: the older call says sent {sent}, received {recv}; later {after:?}");
        }
    }

    /// An adapter's counts asked again, by its index.
    fn cards_now(index: u32) -> (u64, u64) {
        cards().unwrap().into_iter().find(|card| card.index == index).map_or((0, 0), |card| (card.sent, card.recv))
    }
}
