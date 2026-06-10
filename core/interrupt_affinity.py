"""Interrupt Affinity & MSI Mode Manager — redistribute interrupts off Core 0."""
import json
import os
from core.utils import run_ps, run_cmd, get_logger, backup_registry

log = get_logger("irq")


def _reg(key, val, data, vtype="REG_DWORD"):
    r = run_cmd(["reg", "add", key, "/v", val, "/t", vtype, "/d", data, "/f"])
    return r["ok"]


def get_pci_devices() -> list[dict]:
    """Enumerate PCI devices with their interrupt and MSI status."""
    r = run_ps("""
$devices = Get-PnpDevice -Class Display,Net,USB,Media,System -Status OK -ErrorAction SilentlyContinue |
    Where-Object { $_.InstanceId -like 'PCI*' -or $_.InstanceId -like 'USB*' } |
    Select-Object FriendlyName,InstanceId,Class,Status
$results = @()
foreach ($dev in $devices) {
    $regPath = "HKLM:\\SYSTEM\\CurrentControlSet\\Enum\\$($dev.InstanceId)\\Device Parameters\\Interrupt Management\\MessageSignaledInterruptProperties"
    $msiSupported = $null
    $affinityPath = "HKLM:\\SYSTEM\\CurrentControlSet\\Enum\\$($dev.InstanceId)\\Device Parameters\\Interrupt Management\\Affinity Policy"
    $priority = $null
    $affinity = $null
    if (Test-Path $regPath) {
        $msiSupported = (Get-ItemProperty -Path $regPath -Name MSISupported -ErrorAction SilentlyContinue).MSISupported
    }
    if (Test-Path $affinityPath) {
        $priority = (Get-ItemProperty -Path $affinityPath -Name DevicePriority -ErrorAction SilentlyContinue).DevicePriority
        $affinity = (Get-ItemProperty -Path $affinityPath -Name AssignmentSetOverride -ErrorAction SilentlyContinue).AssignmentSetOverride
    }
    $results += @{
        name = $dev.FriendlyName
        instance_id = $dev.InstanceId
        class = $dev.Class
        msi_supported = $msiSupported
        msi_enabled = ($msiSupported -eq 1)
        priority = $priority
        affinity_mask = $affinity
    }
}
$results | ConvertTo-Json -Depth 3
""")
    if r["ok"] and r["out"]:
        try:
            data = json.loads(r["out"])
            if not isinstance(data, list):
                data = [data]
            return data
        except Exception:
            pass
    return []


def get_cpu_core_count() -> int:
    """Get logical processor count."""
    r = run_ps("(Get-CimInstance Win32_Processor).NumberOfLogicalProcessors")
    if r["ok"] and r["out"]:
        try:
            return int(r["out"].strip())
        except ValueError:
            pass
    return os.cpu_count() or 4


def enable_msi_mode(instance_id: str) -> dict:
    """Enable MSI mode for a specific device."""
    log.info(f"Enabling MSI mode for: {instance_id[:40]}...")
    msi_key = rf"HKLM\SYSTEM\CurrentControlSet\Enum\{instance_id}\Device Parameters\Interrupt Management\MessageSignaledInterruptProperties"
    backup_registry(
        rf"HKLM\SYSTEM\CurrentControlSet\Enum\{instance_id}\Device Parameters",
        f"irq_{instance_id[:20].replace(chr(92), '_')}"
    )
    ok = _reg(msi_key, "MSISupported", "1")
    log.info(f"  {'✓' if ok else '✗'} MSI mode enabled")
    return {"ok": ok, "instance_id": instance_id, "reboot_required": True}


def disable_msi_mode(instance_id: str) -> dict:
    """Disable MSI mode for a specific device."""
    log.info(f"Disabling MSI mode for: {instance_id[:40]}...")
    msi_key = rf"HKLM\SYSTEM\CurrentControlSet\Enum\{instance_id}\Device Parameters\Interrupt Management\MessageSignaledInterruptProperties"
    ok = _reg(msi_key, "MSISupported", "0")
    return {"ok": ok, "instance_id": instance_id, "reboot_required": True}


def set_device_affinity(instance_id: str, core: int) -> dict:
    """Set interrupt affinity to a specific CPU core."""
    log.info(f"Setting interrupt affinity to core {core} for: {instance_id[:40]}...")
    affinity_key = rf"HKLM\SYSTEM\CurrentControlSet\Enum\{instance_id}\Device Parameters\Interrupt Management\Affinity Policy"

    # Create affinity mask — binary mask for the specific core
    mask = 1 << core

    backup_registry(
        rf"HKLM\SYSTEM\CurrentControlSet\Enum\{instance_id}\Device Parameters",
        f"affinity_{instance_id[:20].replace(chr(92), '_')}"
    )

    # DevicePolicy = 4 (IrqPolicySpecifiedProcessors)
    ok1 = _reg(affinity_key, "DevicePolicy", "4")
    # AssignmentSetOverride = bitmask as binary REG_BINARY
    # For simplicity, write as DWORD if core < 32
    ok2 = _reg(affinity_key, "AssignmentSetOverride", str(mask))
    ok = ok1 and ok2
    log.info(f"  {'✓' if ok else '✗'} Affinity set to core {core} (mask=0x{mask:x})")
    return {"ok": ok, "core": core, "mask": mask, "reboot_required": True}


def _force_write_enum_dword(key_path: str, value_name: str, value_data: int) -> tuple[bool, int | None, str]:
    """Write a DWORD into an HKLM\\SYSTEM\\CurrentControlSet\\Enum subkey,
    forcing ownership + ACL through PowerShell first.

    HKLM\\Enum is owned by SYSTEM and Administrators have *read-only*
    access by default — `reg add` silently fails (or partially writes
    that the kernel ignores at next boot).  This helper takes the
    standard takeown-then-grant-FullControl path, writes the DWORD,
    and reads it back to verify the write actually persisted.

    Returns (verified_ok, value_after_write, error_message).
    """
    ps_path = key_path.replace(r"HKLM\\", "").replace(r"HKLM\\", "")
    if ps_path.startswith("HKLM\\"):
        ps_path = ps_path[5:]
    if ps_path.startswith("HKEY_LOCAL_MACHINE\\"):
        ps_path = ps_path[19:]

    script = (
        "$ErrorActionPreference = 'Stop'\n"
        "$tdef = @'\n"
        "using System;\n"
        "using System.Runtime.InteropServices;\n"
        "public class TokPriv {\n"
        "  [DllImport(\"advapi32.dll\", SetLastError=true)]\n"
        "  public static extern bool OpenProcessToken(IntPtr h, int acc, ref IntPtr tok);\n"
        "  [DllImport(\"advapi32.dll\", SetLastError=true, CharSet=CharSet.Unicode)]\n"
        "  public static extern bool LookupPrivilegeValueW(string host, string name, out long luid);\n"
        "  [StructLayout(LayoutKind.Sequential, Pack=1)]\n"
        "  public struct TokPriv1Luid { public int Count; public long Luid; public int Attr; }\n"
        "  [DllImport(\"advapi32.dll\", SetLastError=true)]\n"
        "  public static extern bool AdjustTokenPrivileges(IntPtr htok, bool disall,\n"
        "    ref TokPriv1Luid newst, int len, IntPtr prev, IntPtr relen);\n"
        "  public const int SE_PRIV_ENABLED = 0x2;\n"
        "  public const int TOKEN_ADJUST = 0x20;\n"
        "  public const int TOKEN_QUERY  = 0x8;\n"
        "  public static bool Enable(string p) {\n"
        "    IntPtr t = IntPtr.Zero;\n"
        "    if (!OpenProcessToken(System.Diagnostics.Process.GetCurrentProcess().Handle,\n"
        "      TOKEN_ADJUST | TOKEN_QUERY, ref t)) return false;\n"
        "    long lu;\n"
        "    if (!LookupPrivilegeValueW(null, p, out lu)) return false;\n"
        "    TokPriv1Luid tp = new TokPriv1Luid { Count = 1, Luid = lu, Attr = SE_PRIV_ENABLED };\n"
        "    return AdjustTokenPrivileges(t, false, ref tp, 0, IntPtr.Zero, IntPtr.Zero);\n"
        "  }\n"
        "}\n"
        "'@\n"
        "Add-Type -TypeDefinition $tdef -ErrorAction SilentlyContinue\n"
        "[TokPriv]::Enable('SeTakeOwnershipPrivilege') | Out-Null\n"
        "[TokPriv]::Enable('SeRestorePrivilege') | Out-Null\n"
        f"$sub = \"{ps_path}\"\n"
        f"$valName = \"{value_name}\"\n"
        f"$valData = [int]{int(value_data)}\n"
        "$hive = [Microsoft.Win32.Registry]::LocalMachine\n"
        # Make sure the path exists; create intermediate keys.
        "$accum = ''\n"
        "foreach ($p in ($sub -split '\\\\')) {\n"
        "  if (-not $p) { continue }\n"
        "  $accum = if ($accum) { \"$accum\\$p\" } else { $p }\n"
        "  if (-not (Test-Path \"HKLM:\\$accum\")) {\n"
        "    try { New-Item -Path \"HKLM:\\$accum\" -Force | Out-Null } catch { }\n"
        "  }\n"
        "}\n"
        # 1) Take ownership.
        "try {\n"
        "  $k = $hive.OpenSubKey($sub,\n"
        "    [Microsoft.Win32.RegistryKeyPermissionCheck]::ReadWriteSubTree,\n"
        "    [System.Security.AccessControl.RegistryRights]::TakeOwnership)\n"
        "  if ($k) {\n"
        "    $acl = $k.GetAccessControl()\n"
        "    $admins = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544')\n"
        "    $acl.SetOwner($admins)\n"
        "    $k.SetAccessControl($acl)\n"
        "    $k.Close()\n"
        "  }\n"
        "} catch { }\n"
        # 2) Grant Administrators FullControl.
        "try {\n"
        "  $k = $hive.OpenSubKey($sub, $true,\n"
        "    [System.Security.AccessControl.RegistryRights]::ChangePermissions)\n"
        "  if ($k) {\n"
        "    $acl = $k.GetAccessControl()\n"
        "    $admins = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544')\n"
        "    $rule = New-Object System.Security.AccessControl.RegistryAccessRule(\n"
        "      $admins, 'FullControl', 'ContainerInherit', 'None', 'Allow')\n"
        "    $acl.AddAccessRule($rule)\n"
        "    $k.SetAccessControl($acl)\n"
        "    $k.Close()\n"
        "  }\n"
        "} catch { }\n"
        # 3) Write.
        "try {\n"
        "  $k = $hive.OpenSubKey($sub, $true)\n"
        "  if (-not $k) { Write-Output 'ERR:could-not-open-for-write'; exit 1 }\n"
        "  $k.SetValue($valName, $valData, [Microsoft.Win32.RegistryValueKind]::DWord)\n"
        "  $k.Close()\n"
        "} catch { Write-Output \"ERR:$($_.Exception.Message)\"; exit 1 }\n"
        # 4) Read back to verify.
        "$rb = (Get-ItemProperty -Path \"HKLM:\\$sub\" -Name $valName -ErrorAction SilentlyContinue).$valName\n"
        "Write-Output \"OK:$rb\"\n"
    )

    r = run_ps(script, timeout=30)
    out = (r.get("out") or "").strip()
    err = (r.get("err") or "").strip()
    if out.startswith("OK:"):
        rb = out[3:].strip()
        try:
            rb_int = int(rb)
        except ValueError:
            return False, None, f"verify: read-back returned non-int '{rb}'"
        return rb_int == int(value_data), rb_int, ""
    if out.startswith("ERR:"):
        return False, None, out[4:]
    return False, None, err or "unknown PowerShell failure"


def _force_delete_enum_value(key_path: str, value_name: str) -> tuple[bool, str]:
    """Delete a value from an HKLM\\Enum subkey via PowerShell with the
    same takeown-then-grant flow, so the deletion actually persists."""
    ps_path = key_path
    if ps_path.startswith("HKLM\\"):
        ps_path = ps_path[5:]
    script = (
        "$ErrorActionPreference = 'SilentlyContinue'\n"
        "$tdef = @'\n"
        "using System; using System.Runtime.InteropServices;\n"
        "public class TokPriv {\n"
        "  [DllImport(\"advapi32.dll\", SetLastError=true)] public static extern bool OpenProcessToken(IntPtr h, int acc, ref IntPtr tok);\n"
        "  [DllImport(\"advapi32.dll\", SetLastError=true, CharSet=CharSet.Unicode)] public static extern bool LookupPrivilegeValueW(string host, string name, out long luid);\n"
        "  [StructLayout(LayoutKind.Sequential, Pack=1)] public struct TokPriv1Luid { public int Count; public long Luid; public int Attr; }\n"
        "  [DllImport(\"advapi32.dll\", SetLastError=true)] public static extern bool AdjustTokenPrivileges(IntPtr htok, bool disall, ref TokPriv1Luid newst, int len, IntPtr prev, IntPtr relen);\n"
        "  public static bool Enable(string p) { IntPtr t = IntPtr.Zero; if (!OpenProcessToken(System.Diagnostics.Process.GetCurrentProcess().Handle, 0x28, ref t)) return false; long lu; if (!LookupPrivilegeValueW(null, p, out lu)) return false; TokPriv1Luid tp = new TokPriv1Luid { Count = 1, Luid = lu, Attr = 0x2 }; return AdjustTokenPrivileges(t, false, ref tp, 0, IntPtr.Zero, IntPtr.Zero); }\n"
        "}\n"
        "'@\n"
        "Add-Type -TypeDefinition $tdef -ErrorAction SilentlyContinue\n"
        "[TokPriv]::Enable('SeTakeOwnershipPrivilege') | Out-Null\n"
        "[TokPriv]::Enable('SeRestorePrivilege') | Out-Null\n"
        f"$sub = \"{ps_path}\"\n"
        f"$valName = \"{value_name}\"\n"
        "$hive = [Microsoft.Win32.Registry]::LocalMachine\n"
        "try { $k = $hive.OpenSubKey($sub, [Microsoft.Win32.RegistryKeyPermissionCheck]::ReadWriteSubTree, [System.Security.AccessControl.RegistryRights]::TakeOwnership); if ($k) { $acl = $k.GetAccessControl(); $a = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544'); $acl.SetOwner($a); $k.SetAccessControl($acl); $k.Close() } } catch { }\n"
        "try { $k = $hive.OpenSubKey($sub, $true, [System.Security.AccessControl.RegistryRights]::ChangePermissions); if ($k) { $acl = $k.GetAccessControl(); $a = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544'); $rule = New-Object System.Security.AccessControl.RegistryAccessRule($a, 'FullControl', 'ContainerInherit', 'None', 'Allow'); $acl.AddAccessRule($rule); $k.SetAccessControl($acl); $k.Close() } } catch { }\n"
        "try { $k = $hive.OpenSubKey($sub, $true); if ($k) { $k.DeleteValue($valName, $false); $k.Close() } } catch { }\n"
        "$rb = (Get-ItemProperty -Path \"HKLM:\\$sub\" -Name $valName -ErrorAction SilentlyContinue).$valName\n"
        "if ($null -eq $rb) { Write-Output 'OK:deleted' } else { Write-Output \"ERR:still-set-to-$rb\" }\n"
    )
    r = run_ps(script, timeout=30)
    out = (r.get("out") or "").strip()
    if out.startswith("OK:"):
        return True, ""
    return False, out or (r.get("err") or "")


def set_device_priority(instance_id: str, priority: int = 3) -> dict:
    """Set interrupt priority (0=Undefined, 1=Low, 2=Normal, 3=High).

    Uses takeown + ACL grant to actually write through HKLM\\Enum's
    default read-only-for-Administrators ACL, then reads the value back
    to verify the write persisted.  Returns ok=False with a clear error
    if the kernel rolled the value back."""
    log.info(f"Setting interrupt priority to {priority} for: {instance_id[:40]}...")
    affinity_key = rf"HKLM\SYSTEM\CurrentControlSet\Enum\{instance_id}\Device Parameters\Interrupt Management\Affinity Policy"
    verified, actual, err = _force_write_enum_dword(
        affinity_key, "DevicePriority", priority,
    )
    log.info(f"  {'✓' if verified else '✗'} Priority {priority} (actual={actual}, err={err!r})")
    return {
        "ok":              verified,
        "priority":        priority,
        "actual_value":    actual,
        "err":             err,
        "reboot_required": True,
    }


def reset_device_priority(instance_id: str) -> dict:
    """Clear the interrupt priority override for a device, reverting it
    to Windows-managed default."""
    log.info(f"Resetting priority for: {instance_id[:40]}...")
    affinity_key = rf"SYSTEM\CurrentControlSet\Enum\{instance_id}\Device Parameters\Interrupt Management\Affinity Policy"
    ok, err = _force_delete_enum_value(affinity_key, "DevicePriority")
    log.info(f"  {'✓' if ok else '✗'} Priority cleared (err={err!r})")
    return {"ok": ok, "instance_id": instance_id, "err": err, "reboot_required": True}


def reset_device_affinity(instance_id: str) -> dict:
    """Reset device interrupt affinity to Windows default."""
    log.info(f"Resetting affinity for: {instance_id[:40]}...")
    affinity_key = rf"HKLM\SYSTEM\CurrentControlSet\Enum\{instance_id}\Device Parameters\Interrupt Management\Affinity Policy"
    r1 = run_cmd(["reg", "delete", affinity_key, "/v", "DevicePolicy", "/f"])
    r2 = run_cmd(["reg", "delete", affinity_key, "/v", "AssignmentSetOverride", "/f"])
    return {"ok": True, "instance_id": instance_id}


# (older simple reset_device_priority removed in v3 — the takeown-aware
#  version is defined further up in this file alongside the write helper.)


# ─── Input devices — mouse / keyboard / gamepad / generic HID ──────────
def _classify_input_kind(name: str, cls: str) -> str:
    """Bucket a device into mouse / keyboard / gamepad / hid_bluetooth /
    hid based on its name + class.  Used for the Kernel-tab UI."""
    nl = (name or "").lower()
    cl = (cls or "").lower()
    if cl == "mouse" or "mouse" in nl or "trackpad" in nl or "touchpad" in nl:
        return "mouse"
    if cl == "keyboard" or "keyboard" in nl:
        return "keyboard"
    # Gamepad detection — be SPECIFIC.  Bare "controller" matches
    # "HID-compliant system controller" (laptop power button etc.) which
    # is NOT a gamepad and should never get an interrupt-priority boost.
    if cl in ("xnacomposite", "xboxcomposite", "xboxnetapisvc"):
        return "gamepad"
    gamepad_signatures = (
        "xbox", "gamepad", "joypad",
        "game controller", "game pad",
        "dualshock", "dualsense", "wireless controller",
        "ps3 controller", "ps4 controller", "ps5 controller",
        "joycon", "joy-con", "pro controller", "switch controller",
        "stadia", "xinput", "logitech f310", "logitech f710",
        "8bitdo", "arcade stick", "fight stick", "racing wheel",
        "thrustmaster", "fanatec",
    )
    if any(t in nl for t in gamepad_signatures):
        return "gamepad"
    if cl == "bluetooth" or "bluetooth" in nl:
        return "hid_bluetooth"
    return "hid"


def get_input_devices() -> list[dict]:
    """Enumerate every currently-connected human input device — mouse,
    keyboard, gamepad, generic HID — with its registry-side interrupt
    priority + MSI mode status.  This is what the Kernel-tab "Input
    Device Priority" UI lists."""
    r = run_ps("""
$classes = @('Mouse','Keyboard','HIDClass','XnaComposite','XboxComposite')
$devices = Get-PnpDevice -PresentOnly -Status OK -ErrorAction SilentlyContinue |
    Where-Object { $classes -contains $_.Class } |
    Select-Object FriendlyName,InstanceId,Class,Status
$results = @()
foreach ($dev in $devices) {
    $msiPath  = "HKLM:\\SYSTEM\\CurrentControlSet\\Enum\\$($dev.InstanceId)\\Device Parameters\\Interrupt Management\\MessageSignaledInterruptProperties"
    $affPath  = "HKLM:\\SYSTEM\\CurrentControlSet\\Enum\\$($dev.InstanceId)\\Device Parameters\\Interrupt Management\\Affinity Policy"
    $msi      = $null
    $priority = $null
    if (Test-Path $msiPath) {
        $msi = (Get-ItemProperty -Path $msiPath -Name MSISupported -ErrorAction SilentlyContinue).MSISupported
    }
    if (Test-Path $affPath) {
        $priority = (Get-ItemProperty -Path $affPath -Name DevicePriority -ErrorAction SilentlyContinue).DevicePriority
    }
    $results += @{
        name        = $dev.FriendlyName
        instance_id = $dev.InstanceId
        class       = $dev.Class
        msi_supported = $msi
        msi_enabled = ($msi -eq 1)
        priority    = $priority
    }
}
$results | ConvertTo-Json -Depth 3
""")
    if not r["ok"] or not r["out"]:
        return []
    try:
        data = json.loads(r["out"])
    except Exception:
        return []
    if not isinstance(data, list):
        data = [data]
    # Tag each one with our input-kind bucket for the UI
    for d in data:
        d["kind"] = _classify_input_kind(d.get("name", ""), d.get("class", ""))
        # Friendlier null → 0 for the UI
        if d.get("priority") is None:
            d["priority"] = 0
    # De-dupe by instance_id (some devices appear twice with multiple parents)
    seen = set()
    out = []
    for d in data:
        iid = d.get("instance_id", "")
        if iid and iid not in seen:
            seen.add(iid)
            out.append(d)
    # Sort: mouse > keyboard > gamepad > hid_bluetooth > hid, alpha within
    order = {"mouse": 0, "keyboard": 1, "gamepad": 2,
             "hid_bluetooth": 3, "hid": 4}
    out.sort(key=lambda d: (order.get(d.get("kind", "hid"), 9),
                            (d.get("name", "") or "").lower()))
    return out


# ─── v3.1 grouped input-device API ───────────────────────────────────────
# Replaces the old "set priority on any HID instance" foot-gun with a
# narrow, intentional flow:
#   • Keyboards & mice — auto-detect every present device and bulk-boost.
#   • Controllers      — listed individually; UI uses the HTML5 Gamepad
#                        API to ask the user to press a button so we boost
#                        only the actual controller they're using (not
#                        every paired-but-asleep DualSense / Xbox pad).

def get_input_devices_grouped() -> dict:
    """Return the same set of devices `get_input_devices()` finds, but
    bucketed for the new UI.  Generic HID and Bluetooth-radio entries
    are dropped from the user-facing lists — those were the foot-guns
    in the v3.0 version of this UI."""
    devices = get_input_devices()
    out = {"keyboards": [], "mice": [], "controllers": []}
    for d in devices:
        kind = d.get("kind")
        if kind == "keyboard":
            out["keyboards"].append(d)
        elif kind == "mouse":
            out["mice"].append(d)
        elif kind == "gamepad":
            out["controllers"].append(d)
    return out


def _boost_many(devices: list[dict]) -> dict:
    """Helper — set priority=High on every device in the list, return summary."""
    boosted = []
    failed  = []
    for d in devices:
        iid = d.get("instance_id")
        if not iid:
            continue
        r = set_device_priority(iid, priority=3)
        if r.get("ok"):
            boosted.append({"name": d.get("name"), "instance_id": iid})
        else:
            failed.append({"name": d.get("name"), "instance_id": iid,
                           "err": r.get("err") or ""})
    return {"ok": True, "boosted": boosted, "failed": failed,
            "reboot_required": bool(boosted)}


def _reset_many(devices: list[dict]) -> dict:
    """Helper — clear priority override on every device in the list."""
    cleared = []
    failed  = []
    for d in devices:
        iid = d.get("instance_id")
        if not iid:
            continue
        r = reset_device_priority(iid)
        if r.get("ok"):
            cleared.append({"name": d.get("name"), "instance_id": iid})
        else:
            failed.append({"name": d.get("name"), "instance_id": iid,
                           "err": r.get("err") or ""})
    return {"ok": True, "cleared": cleared, "failed": failed,
            "reboot_required": bool(cleared)}


def boost_all_keyboards() -> dict:
    """Auto-detect every present keyboard and set its interrupt priority
    to High.  Idempotent — devices already at High are silently no-op."""
    log.info("Boosting all detected keyboards...")
    grouped = get_input_devices_grouped()
    return _boost_many(grouped["keyboards"])


def boost_all_mice() -> dict:
    """Auto-detect every present mouse / trackpad and set priority High."""
    log.info("Boosting all detected mice...")
    grouped = get_input_devices_grouped()
    return _boost_many(grouped["mice"])


def reset_all_keyboards() -> dict:
    log.info("Resetting all keyboard priorities...")
    grouped = get_input_devices_grouped()
    return _reset_many(grouped["keyboards"])


def reset_all_mice() -> dict:
    log.info("Resetting all mouse priorities...")
    grouped = get_input_devices_grouped()
    return _reset_many(grouped["mice"])


def reset_all_controllers() -> dict:
    log.info("Resetting all controller priorities...")
    grouped = get_input_devices_grouped()
    return _reset_many(grouped["controllers"])


def find_controllers_by_vid_pid(vid: str, pid: str) -> list[dict]:
    """Match a VID/PID pair (from the HTML5 Gamepad API's `id` string) to
    every HKLM\\Enum instance that represents the same physical controller.
    A single physical controller often shows up multiple times — once
    under HID\\... (the HID class view) and once under USB\\... (the USB
    interface view).  Both should get boosted for full coverage.

    Gamepad API returns IDs like:
        "Xbox 360 Controller (Vendor: 045e Product: 028e)"
        "Wireless Controller (Vendor: 054c Product: 0ce6)"  (DualSense)
        "Pro Controller (Vendor: 057e Product: 2009)"

    HKLM\\Enum keys look like:
        HID\\VID_054C&PID_0CE6&...
        USB\\VID_054C&PID_0CE6&MI_00\\...

    Returns the list of matching device dicts (zero, one, or more)."""
    if not vid or not pid:
        return []
    needle = f"VID_{vid.upper()}&PID_{pid.upper()}"
    grouped = get_input_devices_grouped()
    out = [d for d in grouped["controllers"]
           if needle in (d.get("instance_id") or "").upper()]
    if out:
        return out
    # Fallback: VID-only match (helps when wireless dongle reports a
    # different PID than the wired version of the same brand controller)
    return [d for d in grouped["controllers"]
            if f"VID_{vid.upper()}" in (d.get("instance_id") or "").upper()]


def list_gamepad_class_instance_ids() -> list[str]:
    """Fast enumeration — returns the InstanceID of every HID-class
    device that looks like a controller.  ~200ms vs the ~30s of the
    full annotated enumerator.  Caller does the ViGEmBus filtering by
    set-diff against IDs we recorded at pad-creation time."""
    r = run_ps(r"""
$classes = @('HIDClass','XnaComposite','XboxComposite')
$devices = Get-PnpDevice -PresentOnly -Status OK -ErrorAction SilentlyContinue |
    Where-Object { $classes -contains $_.Class } |
    Select-Object FriendlyName,InstanceId
$out = @()
foreach ($dev in $devices) {
    $iid = $dev.InstanceId
    $name = $dev.FriendlyName
    if (-not $iid) { continue }
    $iidU = $iid.ToUpper()
    # Pull VID/PID up-front so the filtering gates can use them.
    # Note: must NOT use $pid here — it's a PowerShell automatic variable
    # bound to the current process ID, and assigning to it silently fails.
    # That's the reason every device row was returning PID_95196 (the
    # PowerShell PID) before this rename.
    $devVid = $null
    $devPid = $null
    $m = [regex]::Match($iidU, 'VID_([0-9A-F]{4})&PID_([0-9A-F]{4})')
    if ($m.Success) { $devVid = $m.Groups[1].Value; $devPid = $m.Groups[2].Value }
    $isHidVidPid = ($iidU -match '^HID\\VID_[0-9A-F]{4}&PID_[0-9A-F]{4}')
    # Strong include: name explicitly identifies a gamepad
    $looksLikeGamepad = ($name -match '(?i)(gamepad|game\s+controller|xbox|wolverine|raiju|kishi|dualshock|dualsense|wireless\s+controller|joycon|joy-con|pro\s+controller|switch\s+controller|stadia|8bitdo|fight\s*stick|arcade\s*stick|racing\s+wheel|thrustmaster|fanatec|game\s*pad|joypad|XInput)')
    if (-not ($isHidVidPid -or $looksLikeGamepad)) { continue }
    # Strong EXCLUDE — these are HID devices but NOT gamepads.  Critical
    # to filter audio/phone/system devices out of the HidHide hide list,
    # otherwise we'd cloak the user's headset volume/mute controls from
    # Discord et al. when virtual-controller mode is on.
    if ($name -match '(?i)(headset|headphone|speaker|earbud|microphone|webcam|camera|phone|telephony|system\s+controller|consumer\s+control|sensor|touch|trackpad|touchpad|pen\s+device|digitizer|battery|HID-compliant\s+vendor-defined)') { continue }
    # Generic 'HID-compliant device' entries — keep ONLY if from a known
    # gamepad vendor's VID.  This is the line that filters out the bulk
    # of unnamed HID interfaces from the user's "show devices" list while
    # still catching virtual-controller layers from Razer / Steam / etc.
    if (-not $looksLikeGamepad -and $m.Success) {
        $gamepadVids = @('045E','054C','057E','1532','046D','2DC8','28DE','146B','24C6','1BAD','0F0D','0738','03EB')
        if (-not ($gamepadVids -contains $devVid)) { continue }
    }
    # Force strings — PowerShell's ConvertTo-Json otherwise coerces
    # all-digit VID/PIDs (e.g. '0001') back to integers, which the
    # Python side trips on when it tries to .upper() them.
    $out += @{name=$name; instance_id=$iid; vid=[string]$devVid; pid=[string]$devPid}
}
$out | ConvertTo-Json -Depth 3
""", timeout=15)
    if not r.get("ok") or not r.get("out"):
        return []
    try:
        data = json.loads(r["out"])
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    return data


# Module-level cache.  Populated by gamepad_mapper.py whenever it creates
# a new ViGEmBus virtual pad; consulted by the diagnostic and by the
# HidHide hide-list resolver to make sure we never hide our own virtual.
_known_vigembus_ids: set[str] = set()


def record_known_vigembus_ids(ids: "set[str] | list[str]") -> None:
    """Called by gamepad_mapper after creating a virtual pad — records
    the InstanceID(s) of our ViGEmBus virtual so the HidHide resolver
    knows not to hide them."""
    global _known_vigembus_ids
    _known_vigembus_ids = set(ids or [])


def get_known_vigembus_ids() -> set[str]:
    return set(_known_vigembus_ids)


def get_all_gamepad_class_devices() -> list[dict]:
    """Broader enumerator than `get_input_devices_grouped`.  Returns ANY
    HID-class device that looks remotely like a controller — including
    virtual gamepads spawned by Razer Synapse, Steam Input, DS4Windows,
    x360ce, etc.  Each entry is annotated with `is_vigembus` so the
    caller can decide whether to hide it via HidHide.

    Fast path — uses the IDs recorded at pad creation by
    `record_known_vigembus_ids` so we don't have to walk parent chains
    via PowerShell (which is brutally slow)."""
    raw = list_gamepad_class_instance_ids()
    vigem_ids = get_known_vigembus_ids()
    seen, out = set(), []
    for d in raw:
        iid = (d or {}).get("instance_id")
        if not iid or iid in seen:
            continue
        seen.add(iid)
        out.append({
            "name":        d.get("name") or "",
            "instance_id": iid,
            "class":       "HIDClass",
            "vid":         (d.get("vid") or "") or None,
            "pid":         (d.get("pid") or "") or None,
            "is_vigembus": iid in vigem_ids,
        })
    return out


def get_controllers_to_hide_for_vigem() -> list[dict]:
    """Return the list of controller-class devices that HidHide should
    cloak so games see ONLY the ViGEmBus virtual pad.  Excludes any
    device we've recorded as a ViGEmBus virtual pad (recorded by
    `record_known_vigembus_ids` at pad-creation time).

    Crucially this catches OTHER virtual gamepads — e.g. Razer Synapse's
    'Razer XInput Game Pad', Steam Input's virtual controller, DS4Windows
    leftovers, x360ce wrappers — which would otherwise sit alongside
    ours in the game's controller list and outvote our recoil
    compensation."""
    all_pads = get_all_gamepad_class_devices()
    return [d for d in all_pads if not d.get("is_vigembus")]


def boost_controller_by_vid_pid(vid: str, pid: str) -> dict:
    """Find every interface of the controller matching this VID/PID and
    boost all of them.  Used by the 'press a button to identify' UI."""
    devs = find_controllers_by_vid_pid(vid, pid)
    if not devs:
        return {"ok": False,
                "err": f"No controller matching VID={vid} PID={pid} found in HKLM\\Enum"}
    return _boost_many(devs)


def boost_controller_by_instance_id(instance_id: str) -> dict:
    """Boost a controller chosen from the list.  The instance_id is one
    interface (HID\\... or USB\\...) — we extract the VID/PID and boost
    every matching interface so both views of the same physical controller
    end up at High priority.  Returns the same shape as _boost_many."""
    if not instance_id:
        return {"ok": False, "err": "instance_id required"}
    log.info(f"Boosting controller by instance: {instance_id[:50]}...")

    # Try to widen the boost to every matching interface.  Extract VID/PID
    # from the instance_id (format: "HID\\VID_054C&PID_0CE6&..." or
    # "USB\\VID_054C&PID_0CE6&MI_00\\...").
    iid_upper = instance_id.upper()
    import re
    m = re.search(r"VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", iid_upper)
    if m:
        return boost_controller_by_vid_pid(m.group(1), m.group(2))

    # No VID/PID in the instance_id → fall back to single-device boost.
    r = set_device_priority(instance_id, priority=3)
    if r.get("ok"):
        return {"ok": True,
                "boosted": [{"instance_id": instance_id, "name": ""}],
                "failed": [],
                "reboot_required": True}
    return {"ok": False,
            "boosted": [],
            "failed": [{"instance_id": instance_id, "err": r.get("err") or ""}],
            "err": r.get("err"),
            "reboot_required": True}


def auto_optimize_interrupts() -> list[dict]:
    """
    Automatically optimize interrupt distribution:
    - GPU → Core 2 (off Core 0, dedicated)
    - NIC → Core 3 (dedicated for network)
    - USB → Core 1 (near Core 0 for input devices)
    - Audio → Core 1
    - Enable MSI on all supported devices
    - Set high priority on GPU and USB
    """
    log.info("═══ AUTO-OPTIMIZING INTERRUPTS ═══")
    devices = get_pci_devices()
    core_count = get_cpu_core_count()
    results = []

    # Core assignment map (adjustable based on core count)
    # Reserve Core 0 for system, distribute devices across others
    gpu_core = min(2, core_count - 1)
    nic_core = min(3, core_count - 1)
    usb_core = min(1, core_count - 1)
    audio_core = min(1, core_count - 1)

    for dev in devices:
        iid = dev.get("instance_id", "")
        cls = (dev.get("class", "") or "").lower()
        name = dev.get("name", "")

        if not iid:
            continue

        # Enable MSI on everything
        if not dev.get("msi_enabled"):
            r = enable_msi_mode(iid)
            results.append({"name": f"MSI: {name}", "ok": r["ok"]})

        # Assign cores by device type
        target_core = None
        priority = 2  # Normal default

        if cls == "display" or "gpu" in name.lower() or "nvidia" in name.lower() or "radeon" in name.lower():
            target_core = gpu_core
            priority = 3  # High
        elif cls == "net" or "ethernet" in name.lower() or "wi-fi" in name.lower() or "network" in name.lower():
            target_core = nic_core
            priority = 2
        elif "usb" in cls or "usb" in name.lower():
            target_core = usb_core
            priority = 3  # High for input devices
        elif cls == "media" or "audio" in name.lower() or "sound" in name.lower():
            target_core = audio_core
            priority = 2

        if target_core is not None:
            r = set_device_affinity(iid, target_core)
            results.append({"name": f"Affinity {name} → Core {target_core}", "ok": r["ok"]})
            r2 = set_device_priority(iid, priority)
            results.append({"name": f"Priority {name} → {priority}", "ok": r2["ok"]})

    log.info(f"═══ INTERRUPT OPTIMIZATION COMPLETE — {len(results)} changes ═══")
    return results
