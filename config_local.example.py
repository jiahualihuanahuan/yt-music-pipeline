"""Copy to config_local.py (gitignored) and fill in. Any variable from config.py can be overridden here."""
from pathlib import Path

# BASE_DIR = Path("/data/yt-music")
# MUSIC_DIR = Path("/srv/music")
# ORGANIZED_DIR = Path("/srv/music-organized")

# Metadata sources (empty = source skipped)
LASTFM_API_KEY = ""          # https://www.last.fm/api/account/create
MB_CONTACT = "you@example.com"   # MusicBrainz asks for a contact in the User-Agent

# LLM
# LLM_BASE_URL = "http://localhost:8001/v1"
# LLM_API_KEY = ""
# LLM_MODEL = "Qwen/Qwen3-8B-AWQ"
# USE_REGEX_PARSER = False
# USE_TITLE_RULES = True        # high-precision pre-filter; False = everything to the LLM
# NON_MUSIC_ACTION = "delete"   # "skip" | "move" | "delete" (= move to DELETE_DIR)
# DELETE_ENABLED = True
# DELETE_DIR = Path("/mnt/data/docker/navidrome/_to_delete")
# ARTIST_ALIASES_FILE = Path("/mnt/data/docker/navidrome/artist_aliases.json")
# NON_MUSIC_DIR = Path("/mnt/data/docker/navidrome/non_music")
