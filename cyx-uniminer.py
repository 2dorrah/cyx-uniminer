#!/usr/bin/env python3
"""
CYTHANX UNIFIED ORGANISM MINER
==============================

Single-file Termux-friendly controller that combines the working pieces from
these supplied sources:
  * CYTHANX ORGANISM state engine
  * XMRig process supervision
  * XMRig stdout telemetry (hashrate / accepted / rejected / errors)
  * atomic JSON state persistence
  * automatic restart with bounded exponential backoff

The CYTHANX pipeline is an auxiliary state/telemetry layer. XMRig performs the
actual pool mining algorithm. This program does not replace or impersonate
RandomX or another XMRig proof-of-work algorithm.

No wallet/private key generation is performed. Supply the pool and wallet/worker
identifier explicitly at runtime.

Examples (Termux):
  python cythanx_unified_miner.py --self-test
  python cythanx_unified_miner.py --organism
  python cythanx_unified_miner.py --auto \
      --xmrig ./xmrig \
      --pool pool.example:3333 \
      --user WALLET.WORKER

Optional environment variables:
  CYTHANX_XMRIG, CYTHANX_POOL, CYTHANX_USER, CYTHANX_PASS,
  CYTHANX_ALGO, CYTHANX_THREADS, CYTHANX_STATE, CYTHANX_DATA_DIR

Dependency:
  python -m pip install blake3
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

NETWORK = "CYTHAN-MAINNET"
CHAIN_ID = "CYTHAN-04-BED-BTCADDR"
VECTOR_KEY = b"CYTHAN-MAINNET-V1"

SCRYPT_N = 1024
SCRYPT_R = 1
SCRYPT_P = 1
SCRYPT_DKLEN = 32
PBKDF2_ROUNDS = 2048

DEFAULT_ALGO = "rx/0"
DEFAULT_STATE_FILE = "cythanx_state.json"
DEFAULT_DATA_DIR = "cythanx_miner"
DEFAULT_INTERVAL = 10.0
DEFAULT_RESTART_DELAY = 5.0
MAX_BACKOFF = 60.0

try:
    from blake3 import blake3
except ImportError:
    blake3 = None


# ---------------------------------------------------------------------------
# CYTHANX primitives
# ---------------------------------------------------------------------------

def require_blake3() -> None:
    if blake3 is None:
        raise RuntimeError(
            "Python package 'blake3' is required. Install it with:\n"
            "  python -m pip install blake3"
        )


def blake3_256(data: bytes) -> bytes:
    require_blake3()
    return blake3(data).digest(length=32)


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def double_sha256(data: bytes) -> bytes:
    return sha256(sha256(data))


def scrypt256(data: bytes, salt: Optional[bytes] = None) -> bytes:
    if salt is None:
        salt = data
    return hashlib.scrypt(
        password=data,
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )


def pbkdf2_sha512(data: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha512", data, salt, PBKDF2_ROUNDS, 64
    )


def joaat(data: bytes) -> int:
    h = 0
    for byte in data:
        h = (h + byte) & 0xFFFFFFFF
        h = (h + ((h << 10) & 0xFFFFFFFF)) & 0xFFFFFFFF
        h ^= h >> 6
    h = (h + ((h << 3) & 0xFFFFFFFF)) & 0xFFFFFFFF
    h ^= h >> 11
    h = (h + ((h << 15) & 0xFFFFFFFF)) & 0xFFFFFFFF
    return h & 0xFFFFFFFF


def cythan_vector() -> tuple[bytes, int]:
    vector = blake3_256(
        b"CYTHAN|VECTOR|" + CHAIN_ID.encode() + b"|" + VECTOR_KEY
    )
    return vector, joaat(vector)


def cythanize(payload: bytes, previous: bytes = b"\x00" * 32) -> dict:
    vector, checksum = cythan_vector()
    checksum_bytes = checksum.to_bytes(4, "big")

    state = blake3_256(
        b"CYTHAN|STATE|" + payload + previous + vector + checksum_bytes
    )
    sha_state = double_sha256(b"CYTHAN|SHA256|" + state)
    scrypt_state = scrypt256(b"CYTHAN|SCRYPT|" + sha_state)
    commitment = blake3_256(
        b"CYTHAN|COMMITMENT|"
        + vector
        + checksum_bytes
        + state
        + sha_state
        + scrypt_state
    )
    return {
        "vector": vector.hex(),
        "joaat": f"{checksum:08x}",
        "state": state.hex(),
        "sha256": sha_state.hex(),
        "scrypt": scrypt_state.hex(),
        "commitment": commitment.hex(),
    }


# ---------------------------------------------------------------------------
# CYTHANX organism engine
# ---------------------------------------------------------------------------

def organism_enigma(data: bytes) -> bytes:
    def rotor(buf: bytes, value: int) -> bytes:
        out = bytearray(len(buf))
        for i, byte in enumerate(buf):
            x = (byte + value + i * 17) & 0xFF
            x ^= (value * 29 + i * 7) & 0xFF
            out[i] = ((x << 3) | (x >> 5)) & 0xFF
        return bytes(out)

    x = rotor(data, 11)
    x = rotor(x, 37)
    x = rotor(x, 73)
    x = bytes(255 - b for b in x)
    x = rotor(x, 73)
    x = rotor(x, 37)
    return rotor(x, 11)


def organism_joatt(data: bytes, previous: bytes = b"") -> dict:
    root = blake3_256(data + previous)
    tree = double_sha256(root + data)
    stretched = pbkdf2_sha512(tree, b"CYTHANX|JOATT|2048")
    memory = scrypt256(stretched[:32], b"CYTHANX|MEMORY")
    mix = blake3_256(root + tree + memory)
    return {
        "root": root.hex(),
        "sha256d": tree.hex(),
        "memory": memory.hex(),
        "state256": mix.hex(),
        "identifier64": mix[:8].hex(),
        "adler32": f"{zlib.adler32(mix) & 0xFFFFFFFF:08x}",
    }


def organism_cythanize(data: bytes, domain: bytes = b"VECTOR") -> dict:
    vector = blake3_256(b"CYTHANX|" + domain + b"|" + data)
    digest = double_sha256(vector)
    work = scrypt256(digest, b"CYTHANX|VECTOR|SCRYPT")
    commitment = blake3_256(vector + digest + work)
    return {
        "vector": vector.hex(),
        "sha256d": digest.hex(),
        "scrypt": work.hex(),
        "commitment": commitment.hex(),
    }


def organism_transmogrify(data: bytes, rounds: int = 3) -> bytes:
    x = data
    for i in range(rounds):
        x = organism_enigma(x)
        x = blake3_256(b"CYTHANX|TRANSMOGRIFY|" + bytes([i & 0xFF]) + x)
        x = scrypt256(x, f"round-{i}".encode())
    return x


@dataclass
class OrganismState:
    energy: float = 1.0
    entropy: float = 0.0
    health: float = 1.0
    generation: int = 0

    def perceive(self, payload: bytes) -> float:
        x = int.from_bytes(blake3_256(payload)[:8], "big") / 2**64
        self.entropy = -math.log2(max(x, 2**-64)) / 64.0
        return x

    def adapt(self, signal: bytes) -> None:
        x = self.perceive(signal)
        learning = 0.02 + 0.08 * x
        self.energy = max(0.0, min(2.0, self.energy * (0.98 + learning)))
        self.health = max(
            0.0,
            min(1.0, 0.97 * self.health + 0.03 * (1.0 - abs(x - 0.5))),
        )
        self.generation += 1


def run_organism(seed: str = "CYTHANX", rounds: int = 8) -> dict:
    if not 1 <= rounds <= 1000:
        raise ValueError("rounds must be between 1 and 1000")

    seed_bytes = seed.encode("utf-8")
    header = (
        b"CYTHANX-HEADER"
        + hashlib.sha256(seed_bytes).digest()
        + blake3_256(seed_bytes)
        + int(time.time()).to_bytes(4, "big")
        + b"\x00" * 24
    )[:80].ljust(80, b"\x00")

    state = blake3_256(b"CYTHANX|GENESIS|" + seed_bytes)
    organism = OrganismState()
    previous = b""
    history = []

    for i in range(rounds):
        jo = organism_joatt(organism_enigma(header + state), previous)
        vector = organism_cythanize(bytes.fromhex(jo["state256"]), b"ORGANISM")
        transformed = organism_transmogrify(
            bytes.fromhex(vector["commitment"]), 3 + (i % 3)
        )
        aux128 = blake3_256(b"CYTHANX|AUX128|" + transformed)[:16]
        state = blake3_256(
            b"CYTHANX|DYNO256|" + state + aux128 + i.to_bytes(8, "big")
        )
        organism.adapt(aux128)
        final = blake3_256(
            b"CYTHANX|IMPOSSIBLE|"
            + header
            + state
            + bytes.fromhex(vector["commitment"])
            + organism.generation.to_bytes(8, "big")
        )
        previous = bytes.fromhex(jo["state256"])
        sample = int.from_bytes(state[:4], "big") / 0xFFFFFFFF
        history.append(
            {
                "round": i,
                "joatt_id": jo["identifier64"],
                "dyno_difficulty": round(1 + 15 * sample, 8),
                "organism_energy": round(organism.energy, 8),
                "organism_health": round(organism.health, 8),
                "final_state": final.hex(),
            }
        )
        header = blake3_256(header + state + final)

    return {
        "name": "CYTHANX ORGANISM",
        "network": NETWORK,
        "chain_id": CHAIN_ID,
        "seed": seed,
        "rounds": rounds,
        "final_dyno_state": state.hex(),
        "organism": asdict(organism),
        "history": history,
    }


def organism_self_test() -> dict:
    result = run_organism("CYTHANX-SELF-TEST", 2)
    assert len(bytes.fromhex(result["final_dyno_state"])) == 32
    assert result["organism"]["generation"] == 2
    return {
        "ok": True,
        "generation": result["organism"]["generation"],
        "final_dyno_state": result["final_dyno_state"],
    }


# ---------------------------------------------------------------------------
# Miner telemetry
# ---------------------------------------------------------------------------

@dataclass
class MinerState:
    running: bool = False
    pid: Optional[int] = None
    hashrate_10s: float = 0.0
    hashrate_60s: float = 0.0
    hashrate_15m: float = 0.0
    accepted: int = 0
    rejected: int = 0
    restarts: int = 0
    errors: int = 0
    cycles: int = 0
    last_error: str = ""
    last_message: str = ""
    last_dyno_state: str = ""
    started_at: Optional[float] = None
    stopped_at: Optional[float] = None


HASHRATE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*H/s", re.IGNORECASE)
ACCEPTED_RE = re.compile(r"(accepted|share accepted)", re.IGNORECASE)
REJECTED_RE = re.compile(r"(rejected|share rejected)", re.IGNORECASE)
ERROR_RE = re.compile(
    r"(error|failed|invalid|connection refused|connection closed|timeout|no connection)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Unified controller
# ---------------------------------------------------------------------------

class UnifiedController:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.state = MinerState()
        self.started_at = time.time()
        self.last_result: Optional[dict] = None
        self.process: Optional[subprocess.Popen] = None

        self.data_dir = Path(args.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = Path(args.state_file)
        self.log_file = self.data_dir / "xmrig.log"
        self.organism_file = self.data_dir / "organism.jsonl"
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

    def log(self, message: str) -> None:
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
        print(line, flush=True)
        try:
            with self.log_file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as exc:
            print(f"[CYTHANX] log write failed: {exc}", file=sys.stderr)

    def save_state(self, event: str) -> None:
        with self.lock:
            payload = {
                "controller": "CYTHANX-UNIFIED-MINER",
                "network": NETWORK,
                "chain_id": CHAIN_ID,
                "event": event,
                "timestamp": int(time.time()),
                "uptime_seconds": round(time.time() - self.started_at, 3),
                "miner": asdict(self.state),
                "organism": (
                    self.last_result.get("organism", {})
                    if self.last_result else {}
                ),
                "final_dyno_state": (
                    self.last_result.get("final_dyno_state", "")
                    if self.last_result else ""
                ),
                "last_history": (
                    self.last_result.get("history", [])[-1]
                    if self.last_result and self.last_result.get("history")
                    else {}
                ),
            }
        tmp = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self.state_file)
        except OSError as exc:
            print(f"[CYTHANX] state write failed: {exc}", file=sys.stderr)

    def append_organism(self, result: dict) -> None:
        record = {
            "timestamp": int(time.time()),
            "final_dyno_state": result["final_dyno_state"],
            "organism": result["organism"],
            "last_history": result["history"][-1] if result["history"] else {},
        }
        try:
            with self.organism_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        except OSError as exc:
            print(f"[CYTHANX] organism log failed: {exc}", file=sys.stderr)

    def organism_loop(self) -> None:
        while not self.stop.is_set():
            try:
                result = run_organism(self.args.seed, self.args.rounds)
                with self.lock:
                    self.last_result = result
                    self.state.cycles += 1
                    self.state.last_dyno_state = result["final_dyno_state"]
                self.append_organism(result)
                self.save_state("organism_cycle")
                org = result["organism"]
                self.log(
                    "[ORGANISM] "
                    f"cycle={self.state.cycles} "
                    f"generation={org['generation']} "
                    f"energy={org['energy']:.6f} "
                    f"health={org['health']:.6f} "
                    f"entropy={org['entropy']:.6f} "
                    f"state={result['final_dyno_state'][:24]}..."
                )
            except Exception as exc:
                self.log(f"[ORGANISM] error: {exc}")
            self.stop.wait(max(1.0, self.args.interval))

    def parse_line(self, line: str) -> None:
        line = line.rstrip()
        if not line:
            return
        with self.lock:
            self.state.last_message = line

        matches = HASHRATE_RE.findall(line)
        if matches:
            try:
                value = float(matches[0])
                with self.lock:
                    self.state.hashrate_10s = value
            except ValueError:
                pass

        if ACCEPTED_RE.search(line):
            with self.lock:
                self.state.accepted += 1
            self.log("[XMRIG] SHARE ACCEPTED: " + line)
            self.save_state("share_accepted")

        if REJECTED_RE.search(line):
            with self.lock:
                self.state.rejected += 1
            self.log("[XMRIG] SHARE REJECTED: " + line)
            self.save_state("share_rejected")

        if ERROR_RE.search(line):
            with self.lock:
                self.state.errors += 1
                self.state.last_error = line
            self.log("[XMRIG] ERROR: " + line)
            self.save_state("xmrig_error")

    def read_output(self, process: subprocess.Popen) -> None:
        if process.stdout is None:
            return
        for raw in process.stdout:
            if self.stop.is_set():
                break
            self.parse_line(str(raw))

    @staticmethod
    def resolve_xmrig(requested: Optional[str]) -> str:
        candidate = requested or os.environ.get("CYTHANX_XMRIG") or "xmrig"
        found = shutil.which(candidate)
        if found:
            return found
        direct = Path(candidate).expanduser()
        if direct.is_file():
            if not os.access(direct, os.X_OK):
                raise RuntimeError(
                    f"XMRig exists but is not executable: {direct}. Run: chmod +x {direct}"
                )
            return str(direct)
        raise FileNotFoundError(
            "XMRig was not found. Put xmrig on PATH or pass --xmrig /path/to/xmrig."
        )

    def build_command(self, xmrig: str) -> list[str]:
        pool = self.args.pool or os.environ.get("CYTHANX_POOL")
        user = self.args.user or os.environ.get("CYTHANX_USER")
        password = (
            self.args.password
            if self.args.password is not None
            else os.environ.get("CYTHANX_PASS", "x")
        )
        algo = self.args.algo or os.environ.get("CYTHANX_ALGO", DEFAULT_ALGO)

        if not pool:
            raise ValueError("No pool supplied. Use --pool POOL:PORT or CYTHANX_POOL.")
        if not user:
            raise ValueError("No wallet/worker supplied. Use --user WALLET.WORKER or CYTHANX_USER.")

        cmd = [
            xmrig,
            "--algo", algo,
            "--url", pool,
            "--user", user,
            "--pass", password or "x",
            "--keepalive",
            "--print-time", "10",
            "--health-print-time", "30",
        ]
        if self.args.tls:
            cmd.append("--tls")
        if self.args.threads:
            cmd += ["--threads", str(self.args.threads)]
        if self.args.rig_id:
            cmd += ["--rig-id", self.args.rig_id]
        if self.args.config:
            cmd += ["--config", self.args.config]
        return cmd

    def launch_once(self) -> int:
        xmrig = self.resolve_xmrig(self.args.xmrig)
        command = self.build_command(xmrig)
        printable = []
        secret = self.args.password or os.environ.get("CYTHANX_PASS", "")
        for item in command:
            printable.append("***" if secret and item == secret else item)
        self.log("[XMRIG] starting: " + " ".join(printable))

        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self.process = process
        with self.lock:
            self.state.running = True
            self.state.pid = process.pid
            self.state.started_at = time.time()
        self.save_state("xmrig_start")

        reader = threading.Thread(
            target=self.read_output,
            args=(process,),
            daemon=True,
            name="xmrig-output",
        )
        reader.start()

        while not self.stop.is_set():
            code = process.poll()
            if code is not None:
                reader.join(timeout=2)
                with self.lock:
                    self.state.running = False
                    self.state.pid = None
                    self.state.stopped_at = time.time()
                self.save_state(f"xmrig_exit_{code}")
                return int(code)
            time.sleep(0.5)

        self.stop_process(process)
        reader.join(timeout=2)
        with self.lock:
            self.state.running = False
            self.state.pid = None
            self.state.stopped_at = time.time()
        self.save_state("xmrig_shutdown")
        return 0

    def stop_process(self, process: Optional[subprocess.Popen] = None) -> None:
        process = process or self.process
        if process is None or process.poll() is not None:
            return
        try:
            process.send_signal(signal.SIGINT)
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        except ProcessLookupError:
            pass

    def supervisor(self) -> int:
        monitor = threading.Thread(
            target=self.organism_loop,
            daemon=True,
            name="cythanx-organism",
        )
        monitor.start()

        backoff = max(0.0, self.args.restart_delay)
        while not self.stop.is_set():
            try:
                exit_code = self.launch_once()
            except Exception as exc:
                self.log(f"[XMRIG] launch error: {exc}")
                exit_code = 127

            if self.stop.is_set():
                break

            with self.lock:
                self.state.restarts += 1
                restart_number = self.state.restarts
            self.save_state(f"xmrig_exit_{exit_code}")

            if self.args.max_restarts >= 0 and restart_number > self.args.max_restarts:
                self.log("[XMRIG] restart limit reached; stopping.")
                break

            self.log(
                f"[XMRIG] exited with code {exit_code}; restart {restart_number} "
                f"in {backoff:.1f}s"
            )
            if self.stop.wait(backoff):
                break
            backoff = min(max(backoff * 2.0, 1.0), MAX_BACKOFF)

        self.stop.set()
        self.stop_process()
        self.save_state("shutdown")
        return 0

    def signal_stop(self, signum, _frame) -> None:
        self.log(f"[CYTHANX] signal {signum}; shutting down...")
        self.stop.set()
        self.stop_process()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="CYTHANX unified organism + XMRig automated miner"
    )
    parser.add_argument("--auto", action="store_true", help="run organism + XMRig supervisor")
    parser.add_argument("--self-test", action="store_true", help="run CYTHANX self-test")
    parser.add_argument("--organism", action="store_true", help="run one organism calculation")
    parser.add_argument("--seed", default=os.environ.get("CYTHANX_SEED", "CYTHANX"))
    parser.add_argument("--rounds", type=int, default=int(os.environ.get("CYTHANX_ROUNDS", "8")))
    parser.add_argument("--interval", type=float, default=float(os.environ.get("CYTHANX_INTERVAL", str(DEFAULT_INTERVAL))))
    parser.add_argument("--state-file", default=os.environ.get("CYTHANX_STATE", DEFAULT_STATE_FILE))
    parser.add_argument("--data-dir", default=os.environ.get("CYTHANX_DATA_DIR", DEFAULT_DATA_DIR))

    parser.add_argument("--xmrig", default=None, help="XMRig executable")
    parser.add_argument("--pool", default=None, help="pool host:port")
    parser.add_argument("--user", default=None, help="wallet or wallet.worker")
    parser.add_argument("--password", default=None, help="pool password; default x")
    parser.add_argument("--algo", default=None, help=f"XMRig algorithm; default {DEFAULT_ALGO}")
    parser.add_argument("--tls", action="store_true", help="enable TLS for the pool")
    parser.add_argument("--threads", type=int, default=None, help="XMRig CPU threads")
    parser.add_argument("--rig-id", default=None, help="optional XMRig rig id")
    parser.add_argument("--config", default=None, help="optional XMRig config file")
    parser.add_argument("--restart-delay", type=float, default=float(os.environ.get("CYTHANX_RESTART_DELAY", str(DEFAULT_RESTART_DELAY))))
    parser.add_argument("--max-restarts", type=int, default=int(os.environ.get("CYTHANX_MAX_RESTARTS", "-1")), help="-1 means unlimited")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        require_blake3()
        if args.rounds < 1 or args.rounds > 1000:
            raise ValueError("--rounds must be between 1 and 1000")
        if args.threads is not None and args.threads <= 0:
            raise ValueError("--threads must be greater than zero")

        if args.self_test:
            print(json.dumps(organism_self_test(), indent=2))
            return 0

        if args.organism:
            print(json.dumps(run_organism(args.seed, args.rounds), indent=2))
            return 0

        if not args.auto:
            print(
                "CYTHANX ready.\n\n"
                "Quick tests:\n"
                "  python cythanx_unified_miner.py --self-test\n"
                "  python cythanx_unified_miner.py --organism\n\n"
                "Automated mining:\n"
                "  python cythanx_unified_miner.py --auto "
                "--xmrig ./xmrig --pool POOL:PORT --user WALLET.WORKER"
            )
            return 0

        controller = UnifiedController(args)
        signal.signal(signal.SIGINT, controller.signal_stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, controller.signal_stop)
        return controller.supervisor()

    except KeyboardInterrupt:
        print("\n[CYTHANX] interrupted")
        return 130
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
