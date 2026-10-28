from __future__ import annotations
import os
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, field_validator
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / '.env')

class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    symbols: list[str] = Field(default_factory=lambda: ['BTCUSDT','ETHUSDT','SOLUSDT'], min_length=1, max_length=12)
    auto_scanner: bool = Field(default_factory=lambda: os.getenv('AUTO_SCANNER', 'false').lower() == 'true')
    scanner_limit: int = Field(default=30, ge=3, le=40)
    ai_shortlist: int = Field(default=4, ge=1, le=6)
    cfd_enabled: bool = False
    cfd_leverage: float = Field(default=2, ge=1, le=5)
    allow_short: bool = True
    cfd_financing_daily: float = Field(default=0.001, ge=0.0001, le=0.01)
    cfd_scan_limit: int = Field(default=20, ge=3, le=40)
    max_positions: int = Field(default=3, ge=1, le=6)
    max_trade_risk_pct: float = Field(default=1.4, gt=0, le=1.4)
    max_portfolio_risk_pct: float = Field(default=2.0, gt=0, le=4.0)
    risk_pct: float = Field(default=1.0, gt=0, le=1.4)
    daily_loss_pct: float = Field(default=2.0, gt=0, le=2)
    min_rr: float = Field(default=2.0, ge=2, le=20)
    fee_rate: float = Field(default=.001, ge=.0001, le=.01)
    cfd_fee_rate: float = Field(default=0., ge=0, le=.01)
    slippage_bps: float = Field(default=5, ge=0, le=100)
    max_spread_bps: float = Field(default=15, gt=0, le=100)
    max_atr_pct: float = Field(default=3, gt=0, le=10)
    entry_tolerance_atr: float = Field(default=0.75, ge=0.25, le=1.0)
    model: str = Field(default=os.getenv('OPENAI_MODEL','gpt-4o-mini'), min_length=1, max_length=80, pattern=r'^[a-zA-Z0-9_.:-]+$')
    max_daily_calls: int = Field(default=288, ge=0, le=288)
    max_daily_tokens: int = Field(default=2000000, ge=0, le=5000000)

    @field_validator('symbols')
    @classmethod
    def valid_symbols(cls, values):
        import re
        values = [v.upper().replace('/','').strip() for v in values]
        if len(set(values)) != len(values) or any(not re.fullmatch(r'[A-Z0-9]{2,16}USDT', v) for v in values):
            raise ValueError('Marchés Spot USDT uniques requis, ex. BTCUSDT.')
        return values

class Environment:
    def __init__(self):
        self.api_key = os.getenv('OPENAI_API_KEY','').strip()
        self.model = os.getenv('OPENAI_MODEL','gpt-4o-mini').strip()
        self.data_dir = Path(os.getenv('DATA_DIR', str(ROOT/'data'))).expanduser()
        if not self.data_dir.is_absolute(): self.data_dir = ROOT/self.data_dir
        self.binance_url = os.getenv('BINANCE_BASE_URL','https://data-api.binance.vision').rstrip('/')
        if self.binance_url not in {'https://data-api.binance.vision'}:
            raise ValueError('BINANCE_BASE_URL doit être un serveur public officiel Binance en HTTPS.')
        self.host = os.getenv('HOST','127.0.0.1')
        self.port = int(os.getenv('PORT','8000'))
        self.password = os.getenv('DASHBOARD_PASSWORD','')
        self.public_origin = os.getenv('PUBLIC_ORIGIN', os.getenv('RENDER_EXTERNAL_URL','')).rstrip('/')
        if self.public_origin:
            from urllib.parse import urlsplit
            parsed = urlsplit(self.public_origin)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
                raise ValueError('PUBLIC_ORIGIN doit être une origine HTTPS sans chemin.')
        if self.host not in {'127.0.0.1','localhost','::1'} and len(self.password)<16:
            raise ValueError('Accès réseau : définissez DASHBOARD_PASSWORD (16 caractères minimum), avec HTTPS devant le serveur.')
        if not 1024<=self.port<=65535: raise ValueError('PORT invalide (1024–65535).')

