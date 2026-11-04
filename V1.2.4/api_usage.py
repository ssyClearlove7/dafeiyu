"""按北京时间自然日记录实际外部 API 请求次数，不保存密钥或请求正文。"""
from contextlib import contextmanager
import datetime as dt
import hashlib
from pathlib import Path
import sqlite3

DATABASE = Path(__file__).resolve().parent / 'usage/api-usage.sqlite3'
SHANGHAI = dt.timezone(dt.timedelta(hours=8))


@contextmanager
def database():
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATABASE, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.executescript('''
            -- API 调用统计：每个北京时间自然日按服务商、模型和密钥分别累计请求次数。
            CREATE TABLE IF NOT EXISTS daily_usage (
                day TEXT NOT NULL,          -- 请求发起时的北京时间日期，YYYY-MM-DD。
                provider TEXT NOT NULL,     -- 外部 API 服务商名称。
                model TEXT NOT NULL,        -- 请求模型名；非模型接口使用接口能力名称。
                key_id TEXT NOT NULL,       -- 服务商与密钥的 SHA-256 指纹；无密钥时为空串。
                key_label TEXT NOT NULL,    -- 页面显示的密钥尾号及短指纹，不包含完整密钥。
                calls INTEGER NOT NULL,    -- 实际请求尝试次数；失败和每次重试各计一次。
                PRIMARY KEY (day, provider, model, key_id)
            );
            -- 统计起点：区分没有采集的历史与采集后的零调用日期。
            CREATE TABLE IF NOT EXISTS usage_meta (
                key TEXT PRIMARY KEY,      -- 统计元数据键，started_at 表示采集起点。
                value TEXT NOT NULL        -- 对应元数据值；started_at 使用带时区 ISO 时间。
            );
        ''')
        db.execute('INSERT OR IGNORE INTO usage_meta VALUES (?,?)',
                   ('started_at', dt.datetime.now(SHANGHAI).isoformat(timespec='seconds')))
        with db:
            yield db
    finally:
        db.close()


def record(provider, model, key=''):
    """在已完成本地校验、即将调用 HTTP 客户端时执行；每次重试单独调用。"""
    key = key or ''
    ident = hashlib.sha256((provider + '\0' + key).encode()).hexdigest() if key else ''
    label = '尾号 ' + key[-4:] + ' · ' + ident[:6] if key else '无密钥'
    day = dt.datetime.now(SHANGHAI).date().isoformat()
    with database() as db:
        db.execute('''INSERT INTO daily_usage VALUES (?,?,?,?,?,1)
                      ON CONFLICT(day,provider,model,key_id)
                      DO UPDATE SET calls=calls+1''',
                   (day, provider, model, ident, label))


def summary(days=14):
    if days not in (7, 14, 30):
        raise ValueError('统计范围只支持7、14或30天')
    today = dt.datetime.now(SHANGHAI).date()
    dates = [(today - dt.timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]
    with database() as db:
        rows = [dict(row) for row in db.execute(
            'SELECT * FROM daily_usage WHERE day>=? AND day<=? ORDER BY day,provider,model,key_id',
            (dates[0], dates[-1]))]
        started = db.execute("SELECT value FROM usage_meta WHERE key='started_at'").fetchone()[0]
    return {'days': dates, 'rows': rows, 'started_at': started, 'timezone': 'Asia/Shanghai'}
