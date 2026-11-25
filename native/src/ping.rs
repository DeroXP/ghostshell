//! One echo request to an IPv4 address, through the same Windows API ping.exe
//! uses (IcmpSendEcho), without starting ping.exe.
//!
//! Host names are not looked up here: the app keeps ping.exe for those.

use std::ffi::c_void;
use std::net::Ipv4Addr;

use crate::json::Json;
use crate::win32::{self, read};

const PAYLOAD: [u8; 32] = *b"vispora-native ping payload 0123";
/// ICMP_ECHO_REPLY on 64-bit Windows is 40 bytes; the reply buffer must also
/// hold the payload and, per the documentation, 8 bytes for an ICMP error.
const REPLY_SIZE: usize = 40 + PAYLOAD.len() + 8;
const REPLY_STATUS: usize = 4;
const REPLY_ROUND_TRIP_MS: usize = 8;
const IP_SUCCESS: u32 = 0;

/// The address as IcmpSendEcho wants it: the four octets in network order.
fn as_in_addr(address: Ipv4Addr) -> u32 {
    u32::from_ne_bytes(address.octets())
}

/// (answered?, round trip in ms) from a reply buffer and the number of
/// replies IcmpSendEcho said it stored.
fn parse_reply(replies: u32, buf: &[u8]) -> (bool, u32) {
    let status: u32 = read(buf, REPLY_STATUS).unwrap_or(u32::MAX);
    if replies == 0 || status != IP_SUCCESS {
        return (false, 0);
    }
    (true, read(buf, REPLY_ROUND_TRIP_MS).unwrap_or(0))
}

/// `q: ping` - {"ok": true, "ms": n} when the address answered within the
/// timeout, {"ok": false} when it did not (or answered with an error such as
/// "destination unreachable", which ping.exe prints without a time).
pub fn ping(target: &str, timeout_ms: u32) -> Result<Json, String> {
    let address: Ipv4Addr = target.trim().parse().map_err(|_| format!("not an IPv4 address: {target}"))?;
    // SAFETY: plain call; the handle is closed below on every path.
    let handle = unsafe { win32::IcmpCreateFile() };
    if handle == win32::INVALID_HANDLE_VALUE || handle == 0 {
        // SAFETY: plain call.
        return Err(format!("IcmpCreateFile failed (error {})", unsafe { win32::GetLastError() }));
    }
    let mut reply = [0u64; REPLY_SIZE / 8 + 1]; // (u64s: the reply structure holds pointers)
    let reply_bytes = reply.len() * 8;
    // SAFETY: the payload and reply buffers are valid for the sizes given.
    let replies = unsafe {
        win32::IcmpSendEcho(
            handle,
            as_in_addr(address),
            PAYLOAD.as_ptr() as *const c_void,
            PAYLOAD.len() as u16,
            std::ptr::null(),
            reply.as_mut_ptr().cast(),
            reply_bytes as u32,
            timeout_ms.max(1),
        )
    };
    // SAFETY: `handle` came from IcmpCreateFile.
    unsafe { win32::IcmpCloseHandle(handle) };
    // SAFETY: `reply` owns `reply_bytes` initialised bytes.
    let raw = unsafe { std::slice::from_raw_parts(reply.as_ptr() as *const u8, reply_bytes) };
    Ok(match parse_reply(replies, raw) {
        (true, ms) => Json::object([("ok", true.into()), ("ms", ms.into())]),
        (false, _) => Json::object([("ok", false.into())]),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_address_goes_out_in_network_order() {
        let a = as_in_addr(Ipv4Addr::new(1, 2, 3, 4));
        assert_eq!(a.to_ne_bytes(), [1, 2, 3, 4]);
    }

    #[test]
    fn a_reply_is_read_by_its_status() {
        let mut buf = vec![0u8; REPLY_SIZE];
        buf[REPLY_ROUND_TRIP_MS..REPLY_ROUND_TRIP_MS + 4].copy_from_slice(&12u32.to_le_bytes());
        assert_eq!(parse_reply(1, &buf), (true, 12));
        assert_eq!(parse_reply(0, &buf), (false, 0), "no reply stored: timed out");
        buf[REPLY_STATUS..REPLY_STATUS + 4].copy_from_slice(&11003u32.to_le_bytes()); // IP_DEST_HOST_UNREACHABLE
        assert_eq!(parse_reply(1, &buf), (false, 0));
        assert_eq!(parse_reply(1, &[]), (false, 0));
    }

    #[test]
    fn only_an_ipv4_address_is_taken() {
        for bad in ["example.com", "1.1.1", "1.1.1.256", "::1", "", "1.1.1.1; calc"] {
            assert!(ping(bad, 100).is_err(), "{bad}");
        }
    }

    #[test]
    fn this_machine_answers_itself() {
        let a = ping("127.0.0.1", 1000).unwrap();
        assert_eq!(a["ok"], Json::Bool(true), "{a}");
        assert!(a["ms"].as_u64().unwrap() < 1000);
    }
}
