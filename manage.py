"""
VM Manager - Quan ly VirtualBox VM + SSH
- Tu dong tai Ubuntu 22.04 Cloud Image (chi tai 1 lan, cache lai)
- Tao cloud-init ISO bang Python thuan (khong can tool ngoai)
- Khong can Extension Pack, khong can Vagrant

Chay: python3 vm_manager.py
"""

import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import platform

# --- FIX LỖI VIRTUALBOX TRÊN WINDOWS  ---
def get_windows_vbox_path():
    try:
        import winreg
        registry_key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Oracle\VirtualBox", 0, winreg.KEY_READ)
        value, _ = winreg.QueryValueEx(registry_key, "InstallDir")
        winreg.CloseKey(registry_key)
        if value and os.path.exists(os.path.join(value, "VBoxManage.exe")):
            return value
    except Exception:
        pass 
    return None

if platform.system() == "Windows":
    vbox_path = get_windows_vbox_path()
    if vbox_path:
        vbox_path = vbox_path.rstrip("\\")
        # Bơm đường dẫn xịn vừa tìm được vào hệ thống cho Python nhìn thấy
        if vbox_path not in os.environ["PATH"]:
            os.environ["PATH"] += os.pathsep + vbox_path

def generate_uuid() -> str:
    return str(uuid.uuid4())


# ─── CONFIG ──────────────────────────────────────────────────────────
VM_NAME = "my-test-vm"
MEMORY_MB = 2048
CPUS = 2
OS_TYPE = "Ubuntu_64"

VM_USER = "ubuntu"
VM_PASS = "ubuntu123"  # <-- doi mat khau o day
VM_HOSTNAME = VM_NAME

SSH_HOST = "127.0.0.1"
SSH_PORT = 2222

# Cache base image - chi tai 1 lan, dung lai cho cac VM sau
CACHE_DIR = os.path.expanduser("~/.cache/vm-manager")
BASE_VMDK = "jammy-server-cloudimg-amd64.vmdk"
BASE_URL = "https://cloud-images.ubuntu.com/jammy/current/" + BASE_VMDK
# ─────────────────────────────────────────────────────────────────────

DEFAULT_MEMORY = MEMORY_MB
DEFAULT_CPUS = CPUS


# ═══════════════════════════════════════════════════════════════════
# HELPER
# ═══════════════════════════════════════════════════════════════════


def run(cmd: list, silent=False) -> subprocess.CompletedProcess:
    if not silent:
        print(f"  $ {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 and not silent:
        print(f"  [WARN] {result.stderr.strip()}")
    return result


def check_virtualbox():
    if shutil.which("vboxmanage") is None:
        print("[LOI] Khong tim thay vboxmanage.")
        sys.exit(1)
    result = run(["vboxmanage", "--version"], silent=True)
    print(f"[OK] VirtualBox {result.stdout.strip()} - khong can Extension Pack")


def vm_exists(name: str) -> bool:
    result = run(["vboxmanage", "list", "vms"], silent=True)
    return f'"{name}"' in result.stdout


# ═══════════════════════════════════════════════════════════════════
# STATUS
# ═══════════════════════════════════════════════════════════════════


def get_vm_info(name: str) -> dict:
    result = run(["vboxmanage", "showvminfo", name, "--machinereadable"], silent=True)
    info = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k.strip()] = v.strip().strip('"')
    return info


def get_vm_state(name: str) -> str:
    if not vm_exists(name):
        return "not_found"
    return get_vm_info(name).get("VMState", "unknown")


def print_vm_status(name: str):
    if not vm_exists(name):
        print(f"\n  VM '{name}' chua ton tai.\n")
        return
    info = get_vm_info(name)
    state = info.get("VMState", "unknown")
    label = {
        "running": "[RUNNING]",
        "poweroff": "[STOPPED]",
        "paused": "[PAUSED] ",
        "saved": "[SAVED]  ",
        "aborted": "[ABORTED]",
    }.get(state, f"[{state.upper()}]")
    print()
    print(f"  Ten        : {name}")
    print(f"  Trang thai : {label}")
    print(f"  RAM        : {info.get('memory', '?')} MB")
    print(f"  CPU        : {info.get('cpus', '?')}")
    i = 0
    while f"Forwarding({i})" in info:
        print(f"  Port fwd   : {info[f'Forwarding({i})']}")
        i += 1
    print()


def list_all_vms():
    result = run(["vboxmanage", "list", "vms"], silent=True)
    running = run(["vboxmanage", "list", "runningvms"], silent=True).stdout
    print()
    print("  Danh sach VM:")
    print("  " + "-" * 40)
    if not result.stdout.strip():
        print("  (Chua co VM nao)")
    for line in result.stdout.strip().splitlines():
        n = line.split('"')[1]
        s = "[RUNNING]" if f'"{n}"' in running else "[STOPPED]"
        print(f"  {s}  {n}")
    print()


# ═══════════════════════════════════════════════════════════════════
# TAI BASE IMAGE
# ═══════════════════════════════════════════════════════════════════


def download_base_image() -> str:
    """
    Tai Ubuntu 22.04 Cloud VMDK ve cache.
    Chi tai 1 lan, lan sau dung lai.
    Tra ve duong dan file VMDK.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    dest = os.path.join(CACHE_DIR, BASE_VMDK)

    if os.path.exists(dest):
        size_mb = os.path.getsize(dest) / 1024 / 1024
        print(f"[CACHE] Base image da co: {dest} ({size_mb:.0f} MB)")
        return dest

    print(f"\n[DOWNLOAD] Tai Ubuntu 22.04 Cloud Image...")
    print(f"  URL  : {BASE_URL}")
    print(f"  Luu  : {dest}")
    print(f"  ~600 MB - vui long cho...\n")

    tmp = dest + ".tmp"

    def progress(block_count, block_size, total_size):
        downloaded = block_count * block_size
        if total_size > 0:
            pct = min(downloaded / total_size * 100, 100)
            mb = downloaded / 1024 / 1024
            tot = total_size / 1024 / 1024
            bar = "#" * int(pct / 2) + "-" * (50 - int(pct / 2))
            print(f"\r  [{bar}] {pct:5.1f}%  {mb:.0f}/{tot:.0f} MB", end="", flush=True)

    try:
        urllib.request.urlretrieve(BASE_URL, tmp, reporthook=progress)
        print()
        os.rename(tmp, dest)
        size_mb = os.path.getsize(dest) / 1024 / 1024
        print(f"[OK] Tai xong: {size_mb:.0f} MB\n")
        return dest
    except Exception as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        print(f"\n[LOI] Tai that bai: {e}")
        sys.exit(1)


# ═══════════════════════════════════════════════════════════════════
# TAO CLOUD-INIT ISO (THUAN PYTHON, KHONG CAN TOOL NGOAI)
# ═══════════════════════════════════════════════════════════════════


def _b16(n):
    """Both-endian 16-bit (little + big)."""
    return struct.pack("<H", n) + struct.pack(">H", n)


def _b32(n):
    """Both-endian 32-bit (little + big)."""
    return struct.pack("<I", n) + struct.pack(">I", n)


def _pad(data: bytes, length: int) -> bytes:
    assert len(data) <= length, f"Data {len(data)} > {length}"
    return data + b"\x00" * (length - len(data))


def _dir_record(name_bytes: bytes, sector: int, size: int, is_dir=False) -> bytes:
    """Tao ISO 9660 directory record."""
    flags = 0x02 if is_dir else 0x00
    name_len = len(name_bytes)
    rec_len = 33 + name_len
    if rec_len % 2:
        rec_len += 1  # pad cho chan

    t = time.localtime()
    dt = bytes(
        [t.tm_year - 1900, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec, 0]
    )

    r = bytes([rec_len, 0])  # record length, ext_attr_len
    r += _b32(sector)  # extent location
    r += _b32(size)  # data length
    r += dt  # recording datetime (7 bytes)
    r += bytes([flags, 0, 0])  # flags, unit_size, gap_size
    r += _b16(1)  # volume sequence number
    r += bytes([name_len])  # identifier length
    r += name_bytes
    if len(r) % 2:
        r += b"\x00"  # pad to even
    return r


def build_iso9660(files: dict, label: str = "cidata") -> bytes:
    """
    Tao ISO 9660 image don gian chua cac file trong 'files'.
    files: {"ten_file": b"noi_dung", ...}
    Tra ve bytes cua file ISO.
    """
    SECTOR = 2048

    # Layout:
    # Sector  0-15 : System area (zeros)
    # Sector    16 : Primary Volume Descriptor (PVD)
    # Sector    17 : Volume Descriptor Set Terminator (VDST)
    # Sector    18 : L-Path Table (little endian)
    # Sector    19 : M-Path Table (big endian)
    # Sector    20 : Root Directory
    # Sector   21+ : File data

    PVD_SEC = 16
    VDST_SEC = 17
    LPATH_SEC = 18
    MPATH_SEC = 19
    ROOT_DIR_SEC = 20
    FILES_START = 21

    # Chuan bi danh sach file voi vi tri sector
    file_list = []
    cur_sec = FILES_START
    for name, content in files.items():
        if isinstance(content, str):
            content = content.encode("utf-8")
        sectors = (len(content) + SECTOR - 1) // SECTOR
        file_list.append(
            {
                "name": name,
                "content": content,
                "sector": cur_sec,
                "size": len(content),
            }
        )
        cur_sec += sectors

    total_sectors = cur_sec

    # --- Root directory ---
    dot = _dir_record(b"\x00", ROOT_DIR_SEC, SECTOR, is_dir=True)
    dotdot = _dir_record(b"\x01", ROOT_DIR_SEC, SECTOR, is_dir=True)
    frecs = b"".join(
        _dir_record(f["name"].encode(), f["sector"], f["size"]) for f in file_list
    )
    root_dir = _pad(dot + dotdot + frecs, SECTOR)

    # --- Path table (chi co root) ---
    # L-Path (little endian location)
    lpath_entry = (
        bytes([1, 0]) + struct.pack("<I", ROOT_DIR_SEC) + struct.pack("<H", 1) + b"\x00"
    )
    lpath = _pad(lpath_entry, SECTOR)
    path_tbl_sz = len(lpath_entry)
    # M-Path (big endian location)
    mpath_entry = (
        bytes([1, 0]) + struct.pack(">I", ROOT_DIR_SEC) + struct.pack(">H", 1) + b"\x00"
    )
    mpath = _pad(mpath_entry, SECTOR)

    # --- PVD ---
    t = time.localtime()
    dt_str = (
        "{:04d}{:02d}{:02d}{:02d}{:02d}{:02d}00".format(
            t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec
        ).encode()
        + b"\x00"
    )  # 17 bytes
    zero_dt = b"0000000000000000\x00"  # 17 bytes

    label_field = label.ljust(32).encode("ascii")[:32]
    root_rec_pvd = _dir_record(b"\x00", ROOT_DIR_SEC, SECTOR, is_dir=True)[:34]

    pvd = b"\x01"  # type
    pvd += b"CD001"  # identifier
    pvd += b"\x01"  # version
    pvd += b"\x00"  # unused
    pvd += b" " * 32  # system identifier
    pvd += label_field  # volume identifier (32)
    pvd += b"\x00" * 8  # unused
    pvd += _b32(total_sectors)  # volume space size (8)
    pvd += b"\x00" * 32  # unused / escape sequences
    pvd += _b16(1)  # volume set size (4)
    pvd += _b16(1)  # volume sequence number (4)
    pvd += _b16(SECTOR)  # logical block size (4)
    pvd += _b32(path_tbl_sz)  # path table size (8)
    pvd += struct.pack("<I", LPATH_SEC)  # L-path location (4)
    pvd += struct.pack("<I", 0)  # optional L-path (4)
    pvd += struct.pack(">I", MPATH_SEC)  # M-path location (4)
    pvd += struct.pack(">I", 0)  # optional M-path (4)
    pvd += root_rec_pvd  # root dir record (34)
    pvd += b" " * 128  # volume set identifier
    pvd += b" " * 128  # publisher identifier
    pvd += b" " * 128  # data preparer identifier
    pvd += b" " * 128  # application identifier
    pvd += b" " * 37  # copyright file identifier
    pvd += b" " * 37  # abstract file identifier
    pvd += b" " * 37  # bibliographic file identifier
    pvd += dt_str  # creation date (17)
    pvd += dt_str  # modification date (17)
    pvd += zero_dt  # expiration date (17)
    pvd += zero_dt  # effective date (17)
    pvd += b"\x01"  # file structure version
    pvd += b"\x00"  # unused
    pvd += b"\x00" * 512  # application use
    pvd += b"\x00" * 653  # reserved
    assert len(pvd) == SECTOR, f"PVD size = {len(pvd)} (expected 2048)"

    # --- VDST ---
    vdst = _pad(b"\xff" + b"CD001" + b"\x01", SECTOR)

    # --- Gop lai thanh ISO ---
    iso = b"\x00" * (16 * SECTOR)  # system area
    iso += pvd
    iso += vdst
    iso += lpath
    iso += mpath
    iso += root_dir
    for f in file_list:
        secs = (f["size"] + SECTOR - 1) // SECTOR
        iso += _pad(f["content"], secs * SECTOR)

    return iso


def create_cloudinit_iso(output_path: str, hostname: str, username: str, password: str):
    """
    Tao file ISO cloud-init NoCloud.
    VM se doc ISO nay khi boot lan dau de cai username/password.
    """
    user_data = f"""#cloud-config
users:
  - name: {username}
    plain_text_passwd: '{password}'
    lock_passwd: false
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    groups: sudo
ssh_pwauth: true
chpasswd:
  expire: false
hostname: {hostname}
"""

    meta_data = f"""instance-id: {hostname}-001
local-hostname: {hostname}
"""

    iso_bytes = build_iso9660(
        files={"user-data": user_data, "meta-data": meta_data},
        label="cidata",
    )

    with open(output_path, "wb") as f:
        f.write(iso_bytes)

    print(f"  [OK] Cloud-init ISO: {output_path} ({len(iso_bytes) // 1024} KB)")


# ═══════════════════════════════════════════════════════════════════
# TAO VM
# ═══════════════════════════════════════════════════════════════════


def create_vm(name: str, memory: int, cpus: int):
    """
    Tao VM day du:
    1. Tai Ubuntu Cloud Image (neu chua co trong cache)
    2. Clone disk thanh VDI rieng cho VM nay
    3. Tao cloud-init ISO (thuan Python)
    4. Tao VM shell + gan disk + ISO
    """
    if vm_exists(name):
        print(f"[SKIP] VM '{name}' da ton tai.")
        return

    vm_dir = os.path.join(os.path.expanduser("~/VirtualBox VMs"), name)
    disk_path = os.path.join(vm_dir, f"{name}.vdi")
    iso_path = os.path.join(vm_dir, f"{name}-cloudinit.iso")

    # --- Buoc 1: Tai base image ---
    print("\n[1/5] Kiem tra base image...")
    base_vmdk = download_base_image()

    # --- Buoc 2: Clone disk ---
    print(f"[2/5] Clone disk cho VM '{name}'...")
    os.makedirs(vm_dir, exist_ok=True)

    result = run(
        [
            "vboxmanage",
            "clonemedium",
            base_vmdk,
            disk_path,
            "--format",
            "VDI",
        ]
    )

    if result.returncode != 0:
        print("[LOI] Khong clone duoc disk. Kiem tra lai file VMDK trong cache.")
        print(f"      Cache: {base_vmdk}")
        print("      Thu xoa cache va tai lai: rm ~/.cache/vm-manager/*.vmdk")
        return

    run(["vboxmanage", "closemedium", "disk", base_vmdk], silent=True)
    # Resize disk len 20GB de co du cho
    run(
        ["vboxmanage", "modifymedium", "disk", disk_path, "--resize", "20480"],
        silent=True,
    )

    # --- Buoc 3: Tao cloud-init ISO ---
    print(f"[3/5] Tao cloud-init ISO...")
    create_cloudinit_iso(iso_path, VM_HOSTNAME, VM_USER, VM_PASS)

    # --- Buoc 4: Tao VM shell ---
    print(f"[4/5] Tao VM '{name}'...")
    result = run(
        [
            "vboxmanage",
            "createvm",
            "--name",
            name,
            "--ostype",
            OS_TYPE,
            "--register",
            "--basefolder",
            os.path.expanduser("~/VirtualBox VMs"),
        ]
    )
    if result.returncode != 0:
        print("[LOI] Khong tao duoc VM.")
        return

    run(
        [
            "vboxmanage",
            "modifyvm",
            name,
            "--memory",
            str(memory),
            "--cpus",
            str(cpus),
            "--pae",
            "on",
            "--acpi",
            "on",
            "--ioapic",
            "on",
            "--graphicscontroller",
            "vmsvga",
            "--vram",
            "16",
            "--usb",
            "off",
            "--boot1",
            "disk",  # boot tu disk truoc (cloud image)
            "--boot2",
            "dvd",
            "--boot3",
            "none",
            "--boot4",
            "none",
            "--paravirtprovider",
            "default",
            "--vrde",
            "off",
            "--audio",
            "none",
            "--nic1",
            "nat",
            "--nictype1",
            "Am79C973",
            "--natpf1",
            f"ssh,tcp,,{SSH_PORT},,22",
        ]
    )

    # --- Buoc 5: Gan disk + ISO ---
    print(f"[5/5] Gan disk + cloud-init ISO...")

    # SATA controller cho disk chinh
    run(
        [
            "vboxmanage",
            "storagectl",
            name,
            "--name",
            "SATA Controller",
            "--add",
            "sata",
            "--controller",
            "IntelAhci",
        ]
    )
    run(
        [
            "vboxmanage",
            "storageattach",
            name,
            "--storagectl",
            "SATA Controller",
            "--port",
            "0",
            "--device",
            "0",
            "--type",
            "hdd",
            "--medium",
            disk_path,
        ]
    )

    # IDE controller cho cloud-init ISO
    run(["vboxmanage", "storagectl", name, "--name", "IDE Controller", "--add", "ide"])
    run(
        [
            "vboxmanage",
            "storageattach",
            name,
            "--storagectl",
            "IDE Controller",
            "--port",
            "0",
            "--device",
            "0",
            "--type",
            "dvddrive",
            "--medium",
            iso_path,
        ]
    )

    print(f"""
[OK] VM '{name}' da san sang!
     User     : {VM_USER}
     Password : {VM_PASS}
     SSH      : ssh -p {SSH_PORT} {VM_USER}@{SSH_HOST}

     Buoc tiep theo:
     1. Chon [2] Start VM (headless)
     2. Chon [9] Cho SSH san sang (~60 giay)
     3. Chon [10] SSH vao VM
""")


# ═══════════════════════════════════════════════════════════════════
# DIEU KHIEN VM
# ═══════════════════════════════════════════════════════════════════


def start_vm(name: str, headless=True):
    state = get_vm_state(name)
    if state == "running":
        print(f"[SKIP] VM '{name}' dang chay roi.")
        return
    if state == "not_found":
        print(f"[LOI] VM '{name}' khong ton tai.")
        return
    mode = "headless" if headless else "gui"
    print(f"\n[START] Khoi dong VM '{name}' ({mode})...")
    result = run(["vboxmanage", "startvm", name, "--type", mode])
    if result.returncode == 0:
        print("[OK] VM dang khoi dong. Cho ~60 giay de cloud-init chay xong.")
    else:
        print("[LOI] Khong start duoc VM.")


def stop_vm(name: str, force=False):
    if get_vm_state(name) != "running":
        print(f"[SKIP] VM khong dang chay.")
        return
    action = "poweroff" if force else "acpipowerbutton"
    label = "Force" if force else "Graceful"
    print(f"\n[STOP] {label} shutdown '{name}'...")
    run(["vboxmanage", "controlvm", name, action])
    print("[OK] Lenh stop da gui.")


def pause_vm(name: str):
    if get_vm_state(name) != "running":
        print("[SKIP] VM khong dang chay.")
        return
    run(["vboxmanage", "controlvm", name, "pause"])
    print(f"[OK] VM paused.")


def resume_vm(name: str):
    if get_vm_state(name) != "paused":
        print("[SKIP] VM khong dang pause.")
        return
    run(["vboxmanage", "controlvm", name, "resume"])
    print(f"[OK] VM resumed.")


def delete_vm(name: str):
    if not vm_exists(name):
        print(f"[SKIP] VM '{name}' khong ton tai.")
        return
    if get_vm_state(name) == "running":
        stop_vm(name, force=True)
        time.sleep(2)
    print(f"\n[DELETE] Xoa VM '{name}'...")
    run(["vboxmanage", "unregistervm", name, "--delete"])
    print(f"[OK] Da xoa VM '{name}'.")


# ═══════════════════════════════════════════════════════════════════
# WAIT & SSH
# ═══════════════════════════════════════════════════════════════════


def wait_for_ssh(host=SSH_HOST, port=SSH_PORT, timeout=300):
    """
    Kiem tra SSH THAT SU san sang bang cach doc SSH banner.
    VirtualBox NAT chap nhan TCP ngay lap tuc nen khong the
    chi kiem tra TCP connect - phai doc banner 'SSH-2.0-...'
    """
    print(f"\n[WAIT] Cho SSH tai {host}:{port} (timeout={timeout}s)...")
    print("       (Boot lan dau + cloud-init can 2-3 phut)")
    start = time.time()
    dots = 0
    while time.time() - start < timeout:
        try:
            sock = socket.create_connection((host, port), timeout=5)
            sock.settimeout(5)
            banner = sock.recv(128)
            sock.close()
            if banner.startswith(b"SSH-"):
                elapsed = int(time.time() - start)
                print(f"\n[OK] SSH san sang sau {elapsed}s! Banner: {banner[:30]}")
                return True
        except Exception:
            pass
        elapsed = int(time.time() - start)
        print(f"  [{elapsed:3d}s] Cho cloud-init chay...", end="\r")
        time.sleep(5)
    print(f"\n[TIMEOUT] SSH khong san sang sau {timeout}s.")
    return False


def print_ssh_info():
    print()
    print("  SSH Info:")
    print("  " + "-" * 50)
    print(f"  Host     : {SSH_HOST}")
    print(f"  Port     : {SSH_PORT}")
    print(f"  User     : {VM_USER}")
    print(f"  Password : {VM_PASS}")
    print()
    print(f"  ssh -p {SSH_PORT} {VM_USER}@{SSH_HOST}")
    print("  " + "-" * 50)
    print()


def ssh_connect(name: str):
    if get_vm_state(name) != "running":
        print(f"[LOI] VM khong dang chay.")
        return
    print(f"\n[SSH] Ket noi vao '{name}'...")
    print_ssh_info()
    
    # Cấu hình lệnh SSH chung cho cả 2 hệ điều hành
    ssh_cmd = [
        "ssh",
        "-p",
        str(SSH_PORT),
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=10",
        f"{VM_USER}@{SSH_HOST}",
    ]

    import platform
    if platform.system() == "Windows":
        # Trên Windows: Dùng subprocess để tránh lỗi văng Terminal, kẹt phím
        try:
            subprocess.run(ssh_cmd)
        except KeyboardInterrupt:
            print("\n[INFO] Đã ngắt kết nối SSH.")
    else:
        # Trên Linux/Mac: Dùng os.execvp để tối ưu hiệu năng và giữ I/O nguyên bản
        os.execvp("ssh", ssh_cmd)


def ssh_run_command(command: str) -> str:
    result = subprocess.run(
        [
            "ssh",
            "-p",
            str(SSH_PORT),
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "ConnectTimeout=10",
            f"{VM_USER}@{SSH_HOST}",
            command,
        ],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() or result.stderr.strip()


# ═══════════════════════════════════════════════════════════════════
# MENU
# ═══════════════════════════════════════════════════════════════════


def print_menu():
    state = get_vm_state(VM_NAME)
    print()
    print("  ╔══════════════════════════════════════════╗")
    print("  ║     VM Manager  (no Extension Pack)     ║")
    print("  ╠══════════════════════════════════════════╣")
    print("  ║  VM    : {:32s} ║".format(VM_NAME[:32]))
    print("  ║  State : {:32s} ║".format(state[:32]))
    print("  ╠══════════════════════════════════════════╣")
    print("  ║  1.  Tao VM (tu dong tai + cai dat)      ║")
    print("  ║  2.  Start VM (headless)                 ║")
    print("  ║  3.  Start VM (co GUI)                   ║")
    print("  ║  4.  Stop VM (graceful)                  ║")
    print("  ║  5.  Stop VM (force)                     ║")
    print("  ║  6.  Pause / Resume VM                   ║")
    print("  ║  7.  Xem trang thai VM                   ║")
    print("  ║  8.  Liet ke tat ca VM                   ║")
    print("  ║  9.  Cho SSH san sang                    ║")
    print("  ║  10. SSH vao VM                          ║")
    print("  ║  11. Chay lenh qua SSH                   ║")
    print("  ║  12. In thong tin SSH                    ║")
    print("  ║  13. Xoa VM                              ║")
    print("  ║  0.  Thoat                               ║")
    print("  ╚══════════════════════════════════════════╝")
    print()


def main():
    check_virtualbox()
    while True:
        print_menu()
        choice = input("  Chon: ").strip()

        if choice == "1":
            print()
            name = input(f"  Ten VM [{VM_NAME}]: ").strip() or VM_NAME
            mem = input(f"  RAM MB [{DEFAULT_MEMORY}]: ").strip()
            cpu = input(f"  CPUs  [{DEFAULT_CPUS}]: ").strip()
            create_vm(
                name=name,
                memory=int(mem) if mem.isdigit() else DEFAULT_MEMORY,
                cpus=int(cpu) if cpu.isdigit() else DEFAULT_CPUS,
            )
        elif choice == "2":
            print()
            name = input(f"  Ten VM can Start [{VM_NAME}]: ").strip() or VM_NAME
            start_vm(name, headless=True)
        elif choice == "3":
            print()
            name = input(f"  Ten VM can Start [{VM_NAME}]: ").strip() or VM_NAME
            start_vm(name, headless=False)
        elif choice == "4":
            print()
            name = input(f"  Ten VM can Stop [{VM_NAME}]: ").strip() or VM_NAME
            stop_vm(name, force=False)
        elif choice == "5":
            print()
            name = input(f"  Ten VM can Force Stop [{VM_NAME}]: ").strip() or VM_NAME
            stop_vm(name, force=True)
        elif choice == "6":
            print()
            name = input(f"  Ten VM can Pause/Resume [{VM_NAME}]: ").strip() or VM_NAME
            s = get_vm_state(name)
            if s == "running":
                pause_vm(name)
            elif s == "paused":
                resume_vm(name)
            else:
                print(f"  [SKIP] State hien tai cua '{name}': '{s}'")
        elif choice == "7":
            print()
            name = input(f"  Ten VM can xem [{VM_NAME}]: ").strip() or VM_NAME
            print_vm_status(name)
        elif choice == "8":
            list_all_vms()
        elif choice == "9":
            wait_for_ssh()
        elif choice == "10":
            print()
            name = input(f"  Ten VM can SSH [{VM_NAME}]: ").strip() or VM_NAME
            ssh_connect(name)
        elif choice == "11":
            cmd = input("  Lenh: ").strip()
            if cmd:
                print("\n  Output:")
                for line in ssh_run_command(cmd).splitlines():
                    print(f"  {line}")
                print()
        elif choice == "12":
            print_ssh_info()
        elif choice == "13":
            print()
            name = input(f"  Ten VM can XOA [{VM_NAME}]: ").strip() or VM_NAME
            if input(f"  Xac nhan xoa vinh vien '{name}'? (yes/no): ").strip() == "yes":
                delete_vm(name)
        elif choice == "0":
            print("\n  Bye!\n")
            break
        else:
            print("  [?] Lua chon khong hop le.\n")


if __name__ == "__main__":
    main()
