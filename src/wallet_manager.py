from pathlib import Path
from cryptography.fernet import Fernet
from solders.keypair import Keypair
from .config import ENCRYPTION_KEY, BASE_DIR

fernet     = Fernet(ENCRYPTION_KEY.encode())
WALLET_DIR = Path(BASE_DIR) / "wallets"
WALLET_DIR.mkdir(exist_ok=True)

def _path(uid: int) -> Path:
    return WALLET_DIR / f"{uid}.key"

def create_wallet(uid: int) -> str:
    kp     = Keypair()
    secret = fernet.encrypt(bytes(kp))
    _path(uid).write_bytes(secret)
    return str(kp.pubkey())

def load_wallet(uid: int) -> Keypair | None:
    p = _path(uid)
    if not p.exists():
        return None
    raw = fernet.decrypt(p.read_bytes())
    return Keypair.from_bytes(raw)
