"""All tunable parameters live here. Fill in API keys before first run.

Local overrides: create config_local.py (gitignored) and redefine any
variable you want to change. It is loaded at the bottom of this file.
"""
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
BASE_DIR = Path("/Volumes/AI/music")

# ---------- Paths ----------
STAGING_DIR = BASE_DIR / "staging"          # raw yt-dlp downloads, original filenames kept
PROCESSED_DIR = BASE_DIR / "processed"      # organized: Artist/Album/Title.mp3
ALLOWLIST_PATH = BASE_DIR / "artist_allowlist.json"
ARCHIVE_PATH = BASE_DIR / "downloaded.txt"  # yt-dlp native download archive
LOG_DIR = BASE_DIR / "logs"
LOCAL_DB_PATH = BASE_DIR / "musicbrainz_cache.db"
COOKIES_PATH = BASE_DIR / "cookies.txt"     # optional: exported YouTube cookies

# ---------- Download ----------
AUDIO_FORMAT = "mp3"
AUDIO_QUALITY = "320"          # VBR ~320k
MIN_DURATION = 90              # seconds; filters shorts/trailers
MAX_DURATION = 600             # seconds; filters long mixes/lives
MAX_PLAYLIST_ITEMS = 1       # max downloads per search query
SEARCH_RESULTS = 1            # ytsearchN: how many results to inspect
SEARCH_SLEEP = 1               # seconds between YouTube searches

# ---------- Search queries (keyword-based), weight = relative priority ----------
# Appended with music-oriented terms so YouTube returns songs, not random videos
SEARCH_QUERIES = [
    # Chinese (highest weight)
    ("华语新歌 official", 10), ("华语翻唱精选 歌曲", 10), ("国风新歌 官方", 8), ("粤语新歌 official", 8),
    ("DJ版华语 歌曲", 6), ("华语民谣 新歌", 6), ("华语R&B 歌曲", 6), ("华语摇滚 新歌", 5),
    ("中文歌曲 翻唱 cover", 8), ("华语热门单曲 2025", 9),
    # Korean
    ("韩语新歌 official", 7), ("韩语热门单曲 歌曲", 7), ("K-pop 新歌 official audio", 6),
    # Japanese
    ("J-pop 新歌 official", 7), ("日本热门歌曲 官方", 6), ("J-rock 新歌", 5),
    # European
    ("法语热门歌曲 official", 5), ("德语热门歌曲 歌曲", 5), ("意大利语热门 歌曲", 5),
    ("西班牙语热门 歌曲", 5), ("欧洲流行新歌 official", 5),
    # English-speaking
    ("美国热门新歌 official", 6), ("英国热门歌曲 official audio", 5),
    ("加拿大热门歌曲 official", 5), ("澳洲热门歌曲 official", 5),
    # Latin
    ("reggaeton 热门 official", 5), ("拉丁热门歌曲 official", 5),
]

# ---------- Artist seed lists (top ~20-50 per region, 2025-2026 charts) ----------
# Each entry: (artist_name, region_tag, weight)
ARTIST_QUERIES = [
    # Chinese ~45
    ("周杰伦", "cn", 10), ("林俊杰", "cn", 10), ("薛之谦", "cn", 9),
    ("邓紫棋", "cn", 9), ("陈奕迅", "cn", 9), ("周深", "cn", 9),
    ("汪苏泷", "cn", 8), ("许嵩", "cn", 8), ("毛不易", "cn", 8),
    ("李荣浩", "cn", 8), ("华晨宇", "cn", 8), ("张学友", "cn", 8),
    ("刘德华", "cn", 8), ("王菲", "cn", 8), ("孙燕姿", "cn", 8),
    ("那英", "cn", 7), ("谭维维", "cn", 7), ("张碧晨", "cn", 7),
    ("单依纯", "cn", 7), ("张韶涵", "cn", 7), ("王心凌", "cn", 7),
    ("袁娅维", "cn", 7), ("黄绮珊", "cn", 6), ("张杰", "cn", 7),
    ("刀郎", "cn", 8), ("许冠杰", "cn", 6), ("罗大佑", "cn", 6),
    ("崔健", "cn", 6), ("齐秦", "cn", 6), ("谢霆锋", "cn", 7),
    ("凤凰传奇", "cn", 7), ("TFBOYS", "cn", 6), ("蔡徐坤", "cn", 6),
    ("王一博", "cn", 6), ("肖战", "cn", 6), ("张艺兴", "cn", 6),
    ("鹿晗", "cn", 6), ("beyond", "cn", 7), ("谭咏麟", "cn", 6),
    ("邓丽君", "cn", 6), ("五月天", "cn", 8), ("告五人", "cn", 7),
    ("蔡健雅", "cn", 7), ("田馥甄", "cn", 7), ("梁静茹", "cn", 7),
    ("S.H.E", "cn", 6), ("苏打绿", "cn", 7), ("草东没有派对", "cn", 6),
    ("万能青年旅店", "cn", 6), ("朴树", "cn", 7), ("老狼", "cn", 6),
    ("信乐团", "cn", 6), ("动力火车", "cn", 6), ("宋冬野", "cn", 7),
    ("赵雷", "cn", 7), ("马頹", "cn", 6), ("陈粒", "cn", 7),
    ("房东的猫", "cn", 6), ("花粥", "cn", 6), ("颜人中", "cn", 6),
    ("任然", "cn", 6), ("隔壁老樊", "cn", 6), ("程响", "cn", 6),
    ("A-Lin", "cn", 7), ("萧敬腾", "cn", 7), ("林宥嘉", "cn", 8),
    ("徐佳莹", "cn", 7), ("卢广仲", "cn", 7), ("魏如萱", "cn", 6),
    ("9m88", "cn", 6), ("持修", "cn", 6), ("茄子蛋", "cn", 6),
    ("美秀集团", "cn", 6), ("灭火器", "cn", 6),

    # Korean ~40
    ("BTS", "kr", 9), ("BLACKPINK", "kr", 9), ("Stray Kids", "kr", 9),
    ("IU", "kr", 9), ("aespa", "kr", 8), ("IVE", "kr", 8),
    ("NewJeans", "kr", 8), ("LE SSERAFIM", "kr", 8), ("ENHYPEN", "kr", 8),
    ("ATEEZ", "kr", 8), ("TXT", "kr", 8), ("SEVENTEEN", "kr", 8),
    ("TWICE", "kr", 8), ("Red Velvet", "kr", 7), ("EXO", "kr", 7),
    ("NCT", "kr", 7), ("NCT 127", "kr", 7), ("NCT Dream", "kr", 7),
    ("BIGBANG", "kr", 8), ("G-Dragon", "kr", 8), ("Taeyeon", "kr", 7),
    ("Jennie", "kr", 8), ("Jisoo", "kr", 7), ("Rosé", "kr", 8),
    ("Lisa", "kr", 7), ("Jimin", "kr", 8), ("V", "kr", 7),
    ("Jung Kook", "kr", 8), ("j-hope", "kr", 7), ("RM", "kr", 7),
    ("Lim Young Woong", "kr", 8), ("DAY6", "kr", 7), ("AKMU", "kr", 7),
    ("Epik High", "kr", 7), ("PSY", "kr", 7), ("Taeyang", "kr", 7),
    ("Jay Park", "kr", 7), ("Eric Nam", "kr", 6), ("Baekhyun", "kr", 7),
    ("ITZY", "kr", 7), ("i-dle", "kr", 7), ("BABYMONSTER", "kr", 7),
    ("RIIZE", "kr", 7), ("BOYNEXTDOOR", "kr", 7), ("ILLIT", "kr", 7),

    # Japanese ~40
    ("Mrs. GREEN APPLE", "jp", 9), ("back number", "jp", 8),
    ("Kenshi Yonezu", "jp", 9), ("米津玄師", "jp", 9),
    ("Vaundy", "jp", 8), ("Number_i", "jp", 8), ("RADWIMPS", "jp", 8),
    ("Fujii Kaze", "jp", 8), ("藤井風", "jp", 8), ("Creepy Nuts", "jp", 8),
    ("Ado", "jp", 9), ("YOASOBI", "jp", 9), ("Aimyon", "jp", 8),
    ("あいみょん", "jp", 8), ("Official HIGE DANDISM", "jp", 8),
    ("Yorushika", "jp", 7), ("ヨルシカ", "jp", 7), ("LiSA", "jp", 8),
    ("BABYMETAL", "jp", 7), ("ONE OK ROCK", "jp", 7),
    ("King Gnu", "jp", 7), ("BE:FIRST", "jp", 7), ("HANA", "jp", 7),
    ("Hikaru Utada", "jp", 8), ("宇多田ヒカル", "jp", 8),
    ("Mr.Children", "jp", 7), ("B'z", "jp", 7), ("ARASHI", "jp", 7),
    ("Snow Man", "jp", 7), ("SixTONES", "jp", 6), ("JO1", "jp", 6),
    ("sakanaction", "jp", 7), ("XG", "jp", 7), ("CHANMINA", "jp", 6),
    ("Aimer", "jp", 7), ("Eve", "jp", 7),
    ("ずっと真夜中でいいのに。", "jp", 7), ("ZUTOMAYO", "jp", 7),
    ("Joe Hisaishi", "jp", 6), ("久石譲", "jp", 6),

    # European ~35
    ("Ed Sheeran", "eu", 8), ("Taylor Swift", "eu", 9), ("Coldplay", "eu", 8),
    ("Dua Lipa", "eu", 8), ("Adele", "eu", 8), ("Harry Styles", "eu", 8),
    ("Olivia Rodrigo", "eu", 8), ("Lewis Capaldi", "eu", 7),
    ("Sam Smith", "eu", 7), ("Stormzy", "eu", 7), ("Dave", "eu", 7),
    ("Central Cee", "eu", 7), ("Lola Young", "eu", 7), ("Myles Smith", "eu", 7),
    ("Alex Warren", "eu", 7), ("Benson Boone", "eu", 7),
    ("Calvin Harris", "eu", 7), ("David Guetta", "eu", 7),
    ("Aya Nakamura", "eu", 7), ("Indila", "eu", 7), ("GIMS", "eu", 7),
    ("Ninho", "eu", 6), ("Jul", "eu", 6), ("Louane", "eu", 6),
    ("Angèle", "eu", 6), ("Christine and the Queens", "eu", 6),
    ("Rammstein", "eu", 7), ("Kraftklub", "eu", 6), ("Sarah Connor", "eu", 6),
    ("Leony", "eu", 6), ("Abor & Tynna", "eu", 6), ("Peter Fox", "eu", 6),
    ("Måneskin", "eu", 8), ("Damiano David", "eu", 7), ("Lucio Corsi", "eu", 7),
    ("Annalisa", "eu", 6), ("Tananai", "eu", 6), ("Fedez", "eu", 6),
    ("Gabry Ponte", "eu", 6), ("Ludovico Einaudi", "eu", 6),
    ("Rosalía", "eu", 8), ("Bad Gyal", "eu", 6), ("C. Tangana", "eu", 7),
    ("Aitana", "eu", 7), ("Quevedo", "eu", 7), ("Rels B", "eu", 6), ("Saiko", "eu", 6),
    ("ABBA", "eu", 7), ("Zara Larsson", "eu", 7), ("Tove Lo", "eu", 6),
    ("Kygo", "eu", 6), ("Alan Walker", "eu", 6), ("Stromae", "eu", 7),

    # US ~40
    ("Taylor Swift", "us", 9), ("Kendrick Lamar", "us", 9),
    ("Drake", "us", 9), ("The Weeknd", "us", 9), ("SZA", "us", 8),
    ("Bruno Mars", "us", 8), ("Lady Gaga", "us", 8), ("Billie Eilish", "us", 8),
    ("Sabrina Carpenter", "us", 8), ("Doja Cat", "us", 7),
    ("Post Malone", "us", 7), ("Travis Scott", "us", 7),
    ("Future", "us", 7), ("Metro Boomin", "us", 7), ("Eminem", "us", 7),
    ("Kanye West", "us", 7), ("J. Cole", "us", 7), ("21 Savage", "us", 6),
    ("Nicki Minaj", "us", 7), ("Cardi B", "us", 6), ("Megan Thee Stallion", "us", 6),
    ("GloRilla", "us", 6), ("Sexyy Red", "us", 6), ("Playboi Carti", "us", 7),
    ("Tyler, the Creator", "us", 7), ("Frank Ocean", "us", 7),
    ("Childish Gambino", "us", 7), ("Anderson .Paak", "us", 6),
    ("Chris Brown", "us", 7), ("Usher", "us", 7), ("Mary J. Blige", "us", 7),
    ("Summer Walker", "us", 6), ("Kehlani", "us", 6), ("H.E.R.", "us", 6),
    ("Leon Thomas", "us", 6), ("Teddy Swims", "us", 7),
    ("Morgan Wallen", "us", 7), ("Luke Combs", "us", 6), ("Jelly Roll", "us", 6),
    ("Shaboozey", "us", 6), ("Chappell Roan", "us", 7), ("Tate McRae", "us", 7),
    ("Olivia Dean", "us", 6), ("Addison Rae", "us", 6),

    # Canada ~20
    ("Drake", "ca", 9), ("The Weeknd", "ca", 9), ("Justin Bieber", "ca", 8),
    ("Tate McRae", "ca", 8), ("Shawn Mendes", "ca", 8), ("Céline Dion", "ca", 8),
    ("Michael Bublé", "ca", 7), ("Avril Lavigne", "ca", 7),
    ("Daniel Caesar", "ca", 7), ("Nickelback", "ca", 7),
    ("Bryan Adams", "ca", 7), ("Nelly Furtado", "ca", 7),
    ("Carly Rae Jepsen", "ca", 6), ("Sarah McLachlan", "ca", 6),
    ("Sum 41", "ca", 6), ("Arcade Fire", "ca", 6), ("Grimes", "ca", 6),
    ("Charlotte Cardin", "ca", 6), ("Karan Aujla", "ca", 6),
    ("Shubh", "ca", 5), ("Yung Kai", "ca", 5),

    # Australia ~15
    ("Sia", "au", 8), ("Tame Impala", "au", 8), ("AC/DC", "au", 8),
    ("Troye Sivan", "au", 7), ("The Kid LAROI", "au", 7),
    ("Empire Of The Sun", "au", 7), ("Tones And I", "au", 7),
    ("Kylie Minogue", "au", 7), ("Gotye", "au", 7),
    ("5 Seconds of Summer", "au", 7), ("Vance Joy", "au", 7),
    ("Dean Lewis", "au", 6), ("PNAU", "au", 6), ("Flume", "au", 7),
    ("Guy Sebastian", "au", 6), ("Iggy Azalea", "au", 6),

    # Latin ~25
    ("Bad Bunny", "la", 9), ("Karol G", "la", 9), ("Rauw Alejandro", "la", 8),
    ("Shakira", "la", 8), ("J Balvin", "la", 8), ("Daddy Yankee", "la", 7),
    ("Fuerza Regida", "la", 8), ("Peso Pluma", "la", 8),
    ("Netón Vega", "la", 7), ("Tito Double P", "la", 7),
    ("Junior H", "la", 7), ("Natanael Cano", "la", 6),
    ("Oscar Maydon", "la", 6), ("Luis R Conriquez", "la", 6),
    ("Feid", "la", 7), ("Beéle", "la", 6), ("Ryan Castro", "la", 6),
    ("Jhayco", "la", 6), ("Myke Towers", "la", 7),
    ("Young Miko", "la", 6), ("Emilia", "la", 6),
    ("María Becerra", "la", 6), ("Kali Uchis", "la", 7),
    ("Rosalía", "la", 8), ("C. Tangana", "la", 7),
]

# ---------- Dedup ----------
DEDUP_THRESHOLD = 85  # rapidfuzz score on "title + artist"; >= this = duplicate

# ---------- LLM (vLLM OpenAI-compatible endpoint) ----------
LLM_BASE_URL = "http://192.168.86.35:8001/v1"
LLM_API_KEY = "vllm-secret-key"
LLM_MODEL = "Qwen/Qwen3-8B-AWQ"  # must match the model name served by vLLM
LLM_TIMEOUT = None         # no timeout; let local model finish
LLM_BATCH_SIZE = 15        # short titles; 3080 10GB + 9B Q4 has ~3GB headroom
LLM_RETRIES = 2            # retry failed batches

# ---------- API keys (optional) ----------
LASTFM_API_KEY = "f205c5985189bf0a5bbac030e4dda140"
SPOTIFY_CLIENT_ID = "2f59486b34844ea39f47fba56870c236"
SPOTIFY_CLIENT_SECRET = "24f60614bf0344fd8f119ba0e2e99630"
DISCOGS_TOKEN = ""

# ---------- Misc ----------
USER_AGENT = "music-pipeline/1.0 (personal; contact via GitHub)"
MB_CONTACT = "music-pipeline@localhost"
DRY_RUN = False

# ---------- Local overrides (gitignored) ----------
try:
    from config_local import *  # type: ignore
except ImportError:
    pass
