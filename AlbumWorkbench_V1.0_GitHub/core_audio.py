# -*- coding: utf-8 -*-
"""核心模块：音频扫描、FLAC 标签与内嵌封面读写、纯文本曲目表匹配排序、安全改名与撤销。"""

import base64
import difflib
import json
import os
import re
import shutil
import subprocess
import time
import traceback
import unicodedata

APP_ID = "AlbumWorkbench"
APP_VER = "1.0"

CFG_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), APP_ID)
CFG_FILE = os.path.join(CFG_DIR, "config.json")
LOG_FILE = os.path.join(CFG_DIR, "last_run.log")
UNDO_FILE = os.path.join(CFG_DIR, "last_apply.json")
UNDO_FALLBACK = ".专辑工作台_撤销记录.json"

AUDIO_EXTS = (".flac", ".mp3", ".m4a", ".wav", ".ape", ".ogg", ".wma",
              ".aiff", ".aif", ".opus")

CREATE_NO_WINDOW = 0x08000000


def write_log(text):
    try:
        os.makedirs(CFG_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
    except Exception:
        pass


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception:
        write_log("保存失败 %s:\n%s" % (path, traceback.format_exc()))
        return False


def load_cfg():
    return load_json(CFG_FILE, {}) or {}


def save_cfg(cfg):
    save_json(CFG_FILE, cfg)


def fmt_dur(sec):
    if sec is None:
        return ""
    try:
        sec = int(round(float(sec)))
    except Exception:
        return ""
    if sec <= 0:
        return ""
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%d:%02d:%02d" % (h, m, s)
    return "%02d:%02d" % (m, s)


def natural_key(text):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text or "")]


# --------------------------------------------------------------------- 文本处理
_PUNCT = set(" \u3000\t\r\n-–—_·•.,，。、:：;；!！?？'\"“”‘’()（）[]【】{}《》〈〉"
             "/\\|+*=&%#@~`^$")
_BRACKETS = re.compile(r"[（(【\[《〈][^）)】\]》〉]*[）)】\]》〉]")


def norm_text(text):
    text = unicodedata.normalize("NFKC", text or "").lower()
    return "".join(ch for ch in text if ch not in _PUNCT)


def core_text(text):
    return _BRACKETS.sub("", text or "")


_ILLEGAL = {
    "\\": "＼", "/": "／", ":": "：", "*": "＊", "?": "？",
    "\"": "＂", "<": "＜", ">": "＞", "|": "｜",
}


def safe_name(text):
    text = "".join(_ILLEGAL.get(ch, ch) for ch in (text or ""))
    text = "".join(ch for ch in text if ord(ch) >= 32)
    text = re.sub(r"\s+", " ", text).strip()
    return text.rstrip(". ")


_LEAD_IDX_SPACE = re.compile(r"^(\d{1,3})\s*[.\-–—、:_)）]?\s+(.+)$")
_LEAD_IDX_SEP = re.compile(r"^(\d{1,3})\s*[.\-–—、:_)）]\s*(.+)$")
_TRACK_PREFIX = re.compile(
    r"^\s*(?:[\[\(【]\s*\d{1,3}\s*[\]\)】]\s*|\d{1,3}\s*[-–—_.、:：]\s*|\d{1,3}\s+)")


def split_lead_index(line):
    for pat in (_LEAD_IDX_SPACE, _LEAD_IDX_SEP):
        m = pat.match(line)
        if m:
            return int(m.group(1)), m.group(2).strip()
    return None, line


def strip_track_prefix(text):
    new = _TRACK_PREFIX.sub("", text or "")
    return new.strip() or (text or "").strip()


def split_title_artist(stem):
    parts = (stem or "").split(" - ")
    if len(parts) >= 2:
        artist = parts[-1].strip()
        title = " - ".join(parts[:-1]).strip()
    else:
        title, artist = (stem or "").strip(), ""
    return strip_track_prefix(title), artist


# --------------------------------------------------------------------- FLAC 读写 & 封面提取
def _read_flac_blocks(data):
    if data[:4] != b"fLaC":
        raise ValueError("不是 FLAC 文件")
    pos = 4
    blocks = []
    while True:
        if pos + 4 > len(data):
            raise ValueError("FLAC 元数据不完整")
        head = data[pos]
        last = bool(head & 0x80)
        btype = head & 0x7F
        length = int.from_bytes(data[pos + 1:pos + 4], "big")
        payload = data[pos + 4:pos + 4 + length]
        if len(payload) != length:
            raise ValueError("FLAC 元数据块被截断")
        blocks.append({"type": btype, "payload": payload})
        pos += 4 + length
        if last:
            break
    return blocks, pos


def _streaminfo(payload):
    if len(payload) < 34:
        return None
    b = payload
    sample_rate = (b[10] << 12) | (b[11] << 4) | (b[12] >> 4)
    total_samples = (((b[13] & 0x0F) << 32) | (b[14] << 24) |
                     (b[15] << 16) | (b[16] << 8) | b[17])
    if not sample_rate:
        return None
    return {
        "sample_rate": sample_rate,
        "total_samples": total_samples,
        "duration": (total_samples / float(sample_rate)) if total_samples else None,
    }


def parse_vorbis_comment(payload):
    try:
        n = int.from_bytes(payload[0:4], "little")
        pos = 4
        vendor = payload[pos:pos + n]
        pos += n
        count = int.from_bytes(payload[pos:pos + 4], "little")
        pos += 4
        items = []
        for _ in range(count):
            ln = int.from_bytes(payload[pos:pos + 4], "little")
            pos += 4
            raw = payload[pos:pos + ln]
            pos += ln
            text = raw.decode("utf-8", "replace")
            if "=" in text:
                key, value = text.split("=", 1)
                items.append((key.strip().upper(), value))
        return vendor, items
    except Exception:
        return b"", []


def build_vorbis_comment(items, vendor=b"AlbumWorkbench 1.0"):
    out = bytearray()
    out += len(vendor).to_bytes(4, "little")
    out += vendor
    out += len(items).to_bytes(4, "little")
    for key, value in items:
        raw = ("%s=%s" % (key, value)).encode("utf-8")
        out += len(raw).to_bytes(4, "little")
        out += raw
    return bytes(out)


def _parse_flac_picture(payload):
    """解析 FLAC METADATA_BLOCK_PICTURE (type 6)，返回 (pic_type, mime, bytes) 或 None。"""
    try:
        pos = 0
        pic_type = int.from_bytes(payload[pos:pos + 4], "big")
        pos += 4
        mime_len = int.from_bytes(payload[pos:pos + 4], "big")
        pos += 4
        mime = payload[pos:pos + mime_len].decode("ascii", "replace").lower()
        pos += mime_len
        desc_len = int.from_bytes(payload[pos:pos + 4], "big")
        pos += 4 + desc_len
        pos += 16  # width, height, depth, colors
        data_len = int.from_bytes(payload[pos:pos + 4], "big")
        pos += 4
        img_data = payload[pos:pos + data_len]
        if len(img_data) < 64:
            return None
        return pic_type, mime, img_data
    except Exception:
        return None


def extract_flac_cover(path):
    """从单个 FLAC 文件提取内嵌封面，返回 (img_bytes, ext) 或 (None, None)。"""
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"fLaC":
                return None, None
            candidates = []
            while True:
                head = f.read(4)
                if len(head) < 4:
                    break
                last = bool(head[0] & 0x80)
                btype = head[0] & 0x7F
                length = int.from_bytes(head[1:4], "big")
                if btype == 6:
                    payload = f.read(length)
                    parsed = _parse_flac_picture(payload)
                    if parsed:
                        candidates.append(parsed)
                else:
                    f.seek(length, 1)
                if last:
                    break
        if not candidates:
            return None, None
        # 优先 type=3 (Cover Front)，其次取字节数最大的
        candidates.sort(key=lambda x: (1 if x[0] == 3 else 0, len(x[2])), reverse=True)
        _, mime, img_data = candidates[0]
        if "png" in mime or img_data[:8] == b"\x89PNG\r\n\x1a\n":
            ext = ".png"
        elif "webp" in mime or img_data[:4] == b"RIFF":
            ext = ".webp"
        else:
            ext = ".jpg"
        return img_data, ext
    except Exception:
        return None, None


def extract_folder_embedded_cover(audio_paths, dest_folder):
    """扫描音频列表中的 FLAC，提取第一张内嵌高清封面保存到 dest_folder。"""
    for p in audio_paths:
        if os.path.splitext(p)[1].lower() != ".flac":
            continue
        data, ext = extract_flac_cover(p)
        if data:
            out_path = os.path.join(dest_folder, "cover" + ext)
            with open(out_path, "wb") as f:
                f.write(data)
            return out_path
    return None


def read_flac_meta(path):
    with open(path, "rb") as f:
        if f.read(4) != b"fLaC":
            raise ValueError("不是 FLAC 文件")
        blocks = []
        while True:
            head = f.read(4)
            if len(head) < 4:
                raise ValueError("FLAC 元数据不完整")
            last = bool(head[0] & 0x80)
            btype = head[0] & 0x7F
            length = int.from_bytes(head[1:4], "big")
            if btype in (0, 4):
                blocks.append({"type": btype, "payload": f.read(length)})
            else:
                f.seek(length, 1)
            if last:
                break
    info = {
        "duration": None, "tags": {}, "comment_payload": None,
        "comment_vendor": b"", "had_comment": False,
    }
    for block in blocks:
        if block["type"] == 0:
            si = _streaminfo(block["payload"])
            if si:
                info["duration"] = si["duration"]
        elif block["type"] == 4:
            info["had_comment"] = True
            info["comment_payload"] = block["payload"]
            vendor, items = parse_vorbis_comment(block["payload"])
            info["comment_vendor"] = vendor
            tags = {}
            for key, value in items:
                tags.setdefault(key, value)
            info["tags"] = tags
    return info


def write_flac_comment_payload(path, payload):
    with open(path, "rb") as f:
        data = f.read()
    blocks, audio_off = _read_flac_blocks(data)
    new_blocks = []
    replaced = False
    for block in blocks:
        if block["type"] == 4:
            replaced = True
            if payload is not None:
                new_blocks.append({"type": 4, "payload": payload})
            continue
        new_blocks.append(block)
    if not replaced and payload is not None:
        insert_at = 1 if new_blocks and new_blocks[0]["type"] == 0 else 0
        new_blocks.insert(insert_at, {"type": 4, "payload": payload})

    out = bytearray(b"fLaC")
    for i, block in enumerate(new_blocks):
        if len(block["payload"]) > 0xFFFFFF:
            raise ValueError("标签太长，FLAC 单个元数据块放不下")
        flag = 0x80 if i == len(new_blocks) - 1 else 0x00
        out.append(flag | (block["type"] & 0x7F))
        out += len(block["payload"]).to_bytes(3, "big")
        out += block["payload"]
    out += data[audio_off:]

    tmp = path + ".awb-tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(out)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass


def write_flac_tags(path, tags, vendor=None):
    meta = read_flac_meta(path)
    # 保留原有的其它非覆盖字段（如 DATE / GENRE 等），仅更新目标键
    _, orig_items = parse_vorbis_comment(meta["comment_payload"]) if meta["comment_payload"] else (b"", [])
    update_keys = set(k.upper() for k in tags.keys())
    items = [(k, v) for (k, v) in orig_items if k.upper() not in update_keys]
    for key, value in tags.items():
        if value is None or str(value) == "":
            continue
        items.append((key.upper(), str(value)))
    payload = build_vorbis_comment(items, vendor or meta["comment_vendor"] or b"AlbumWorkbench 1.0")
    write_flac_comment_payload(path, payload)
    return meta["comment_payload"], meta["comment_vendor"], meta["had_comment"]


def restore_flac_comment(path, payload, had_comment):
    if payload is None:
        if had_comment:
            return
        write_flac_comment_payload(path, None)
    else:
        write_flac_comment_payload(path, payload)


# --------------------------------------------------------------------- 其它格式 (ffprobe / ffmpeg)
_TOOLS = {}


def find_tool(name):
    if name in _TOOLS:
        return _TOOLS[name]
    path = shutil.which(name)
    if not path:
        for cand in (
            os.path.join(r"C:\Program Files\FFmpeg", name + ".exe"),
            os.path.join(r"C:\Program Files\FFmpeg\bin", name + ".exe"),
            os.path.join(r"C:\ffmpeg\bin", name + ".exe"),
            os.path.join(r"C:\ffmpeg", name + ".exe"),
            os.path.join(r"D:\ffmpeg\bin", name + ".exe"),
        ):
            if os.path.isfile(cand):
                path = cand
                break
    _TOOLS[name] = path
    return path


def _run_hidden(cmd):
    flags = CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          creationflags=flags)


def probe_audio(path):
    ffprobe = find_tool("ffprobe")
    if not ffprobe:
        return None, {}
    try:
        proc = _run_hidden([ffprobe, "-v", "quiet", "-print_format", "json",
                            "-show_format", path])
        data = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
        fmt = data.get("format") or {}
        dur = None
        try:
            dur = float(fmt.get("duration"))
        except Exception:
            pass
        tags = {str(k).upper(): str(v) for k, v in (fmt.get("tags") or {}).items()}
        return dur, tags
    except Exception:
        return None, {}


def write_tags_ffmpeg(path, tags):
    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("没有找到 ffmpeg，无法写这种格式的标签")
    ext = os.path.splitext(path)[1]
    tmp = os.path.join(os.path.dirname(path), ".awb_tag_tmp" + ext)
    cmd = [ffmpeg, "-y", "-v", "error", "-i", path, "-map", "0", "-c", "copy",
           "-map_metadata", "-1", "-fflags", "+bitexact"]
    for key, value in tags.items():
        if value is None or str(value) == "":
            continue
        cmd += ["-metadata", "%s=%s" % (key, value)]
    cmd.append(tmp)
    proc = _run_hidden(cmd)
    if proc.returncode != 0 or not os.path.exists(tmp):
        raise RuntimeError("ffmpeg 写标签失败")
    os.replace(tmp, path)


def read_audio_info(path):
    name = os.path.basename(path)
    stem, ext = os.path.splitext(name)
    ext = ext.lower()
    title, artist = split_title_artist(stem)
    info = {
        "key": path, "path": path, "name": name, "stem": stem, "ext": ext,
        "title": title, "artist": artist, "dur": None, "tags": {},
        "track_tag": None, "tag_title": "", "tag_artist": "", "album": "",
        "date": "", "genre": "", "conf": None, "unmatched": False,
        "comment_payload": None, "comment_vendor": b"", "had_comment": False,
    }
    try:
        if ext == ".flac":
            meta = read_flac_meta(path)
            info["dur"] = meta["duration"]
            info["tags"] = meta["tags"]
            info["comment_payload"] = meta["comment_payload"]
            info["comment_vendor"] = meta["comment_vendor"]
            info["had_comment"] = meta["had_comment"]
        else:
            dur, tags = probe_audio(path)
            info["dur"] = dur
            info["tags"] = tags
    except Exception:
        write_log("读取音频失败 %s:\n%s" % (path, traceback.format_exc()))

    tags = info["tags"]
    info["tag_title"] = (tags.get("TITLE") or "").strip()
    info["tag_artist"] = (tags.get("ARTIST") or tags.get("ALBUMARTIST") or "").strip()
    info["album"] = (tags.get("ALBUM") or "").strip()
    info["date"] = (tags.get("DATE") or tags.get("YEAR") or tags.get("ORIGINALDATE") or "").strip()
    info["genre"] = (tags.get("GENRE") or "").strip()
    for key in ("TRACKNUMBER", "TRACK", "TRACKNUM"):
        val = tags.get(key)
        if val:
            m = re.match(r"\s*(\d+)", val)
            if m:
                info["track_tag"] = int(m.group(1))
                break
    if info["tag_title"]:
        info["title"] = info["tag_title"]
    if info["tag_artist"]:
        info["artist"] = info["tag_artist"]
    return info


def list_audio_files(folder):
    out = []
    if not folder or not os.path.isdir(folder):
        return out
    for fn in os.listdir(folder):
        if fn.startswith(".") or fn.lower().endswith((".awb-tmp", ".ton-tmp")):
            continue
        full = os.path.join(folder, fn)
        if os.path.isfile(full) and os.path.splitext(fn)[1].lower() in AUDIO_EXTS:
            out.append(full)
    out.sort(key=lambda p: natural_key(os.path.basename(p)))
    return out


# --------------------------------------------------------------------- 纯文本曲目表解析与匹配
_NOISE_WORDS = ("VIP", "SQ", "HQ", "Hi-Res", "Hires", "MV", "视频", "试听", "无损",
                "高清", "独家", "已收藏", "播放", "收藏", "下载", "歌词", "更多",
                "排序", "音质", "杜比", "臻品", "母带")
_NOISE_RE = re.compile(r"(?<![A-Za-z])(" + "|".join(re.escape(w) for w in _NOISE_WORDS) +
                       r")(?![A-Za-z])", re.I)
_HEADER_RE = re.compile(r"^(歌曲\s*\d*|歌手|时长|专辑|作曲|作词|编曲|序号|标题|"
                        r"专辑信息|评论\s*\d*|播放全部|共\s*\d+\s*首|展开|收起|"
                        r"上一页|下一页|全部\s*\d*|歌曲列表|曲目|歌曲名|歌手名|专辑名)$")
_DUR_ONLY = re.compile(r"^(\d{1,3})[:：](\d{2})$")
_DUR_TAIL = re.compile(r"(\d{1,3})[:：](\d{2})$")
_PURE_INDEX = re.compile(r"^\d{1,3}$")


def _mmss(minutes, seconds):
    try:
        return int(minutes) * 60 + int(seconds)
    except Exception:
        return None


def _clean_noise(line):
    cleaned = _NOISE_RE.sub(" ", line)
    cleaned = re.sub(r"[▶►▷♡♥❤★☆→←\u200b]", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def parse_tracklist(text):
    raw_lines = (text or "").replace("\r", "").split("\n")
    cleaned = []
    for line in raw_lines:
        line = line.strip()
        if not line or _HEADER_RE.match(line):
            continue
        line = _clean_noise(line)
        if line:
            cleaned.append(line)

    pure_indexes = sum(1 for line in cleaned if _PURE_INDEX.match(line))
    structured = pure_indexes >= 2

    entries = []
    pending = None
    cur = None

    for line in cleaned:
        if _PURE_INDEX.match(line):
            pending = int(line)
            continue
        if _DUR_ONLY.match(line):
            if cur is not None and cur.get("dur") is None:
                cur["dur"] = _mmss(*_DUR_ONLY.match(line).groups())
            continue

        idx, body = split_lead_index(line)
        dur = None
        m = _DUR_TAIL.search(body)
        if m:
            dur = _mmss(m.group(1), m.group(2))
            body = body[:m.start()].strip()
        body = body.strip(" -–—|·")
        if not body:
            if idx is not None:
                pending = idx
            continue

        title, artist = body, ""
        if " - " in body:
            left, right = body.rsplit(" - ", 1)
            if left.strip() and right.strip():
                title, artist = left.strip(), right.strip()

        if idx is not None:
            cur = {"idx": idx, "title": title, "artist": artist, "dur": dur, "raw": line}
            entries.append(cur)
            pending = None
            continue
        if pending is not None:
            cur = {"idx": pending, "title": title, "artist": artist, "dur": dur, "raw": line}
            entries.append(cur)
            pending = None
            continue
        if structured and cur is not None and cur["dur"] is None and not cur["artist"]:
            cur["artist"] = body
            continue

        cur = {"idx": None, "title": title, "artist": artist, "dur": dur, "raw": line}
        entries.append(cur)

    for i, e in enumerate(entries):
        if e["idx"] is None:
            e["idx"] = i + 1
    return entries


def title_similarity(entry_title, file_title):
    best = 0.0
    for a in (norm_text(entry_title), norm_text(core_text(entry_title))):
        if not a:
            continue
        for b in (norm_text(file_title), norm_text(core_text(file_title))):
            if not b:
                continue
            if a == b:
                return 1.0
            if len(a) >= 2 and len(b) >= 2 and (a in b or b in a):
                best = max(best, 0.97)
            best = max(best, difflib.SequenceMatcher(None, a, b).ratio())
    return best


def pair_score(entry, info):
    score = title_similarity(entry.get("title", ""), info.get("title", ""))
    if entry.get("artist") and info.get("artist"):
        artist_sim = difflib.SequenceMatcher(
            None, norm_text(entry["artist"]), norm_text(info["artist"])).ratio()
        if artist_sim > 0.8:
            score = min(1.0, score + 0.02)
    if entry.get("dur") and info.get("dur"):
        diff = abs(entry["dur"] - info["dur"])
        dur_sim = max(0.0, 1.0 - diff / 8.0)
        score = 0.82 * score + 0.18 * dur_sim
        if diff <= 3:
            score = min(1.0, score + 0.02)
    return score


def match_tracks(entries, files, threshold=0.45):
    pairs = []
    for i, entry in enumerate(entries):
        for j, info in enumerate(files):
            pairs.append((pair_score(entry, info), i, j))
    pairs.sort(reverse=True)
    used_e, used_f, result = set(), set(), {}
    for score, i, j in pairs:
        if i in used_e or j in used_f:
            continue
        if score < threshold:
            break
        result[i] = (j, score)
        used_e.add(i)
        used_f.add(j)
    return result


# --------------------------------------------------------------------- 命名 / 应用 / 撤销
def make_new_stem(index, info, use_tags=True):
    num = ("%02d" % index) if index < 100 else ("%03d" % index)
    title = info.get("tag_title") if use_tags else ""
    artist = info.get("tag_artist") if use_tags else ""
    title = (title or info.get("title") or info.get("stem") or "").strip()
    artist = (artist or info.get("artist") or "").strip()
    base = "%s - %s" % (num, safe_name(title))
    if artist:
        base += " - %s" % safe_name(artist)
    return base


def make_new_name(index, info, use_tags=True):
    return make_new_stem(index, info, use_tags) + info["ext"]


def build_tag_map(info, index, total, album):
    tags = {}
    title = (info.get("title") or info.get("tag_title") or info.get("stem") or "").strip()
    artist = (info.get("artist") or info.get("tag_artist") or "").strip()
    if title:
        tags["TITLE"] = title
    if artist:
        tags["ARTIST"] = artist
    if album:
        tags["ALBUM"] = album
    tags["TRACKNUMBER"] = str(index)
    tags["TRACKTOTAL"] = str(total)
    return tags


def plan_order(order, use_tags=True):
    return [(info, make_new_name(i + 1, info, use_tags)) for i, info in enumerate(order)]


def validate_plan(plan):
    problems = []
    seen = {}
    for i, (info, new_name) in enumerate(plan):
        key = new_name.lower()
        if key in seen:
            problems.append("第 %d 首和第 %d 首编号后重名：%s" % (seen[key] + 1, i + 1, new_name))
        else:
            seen[key] = i
        target = os.path.join(os.path.dirname(info["path"]), new_name)
        if os.path.exists(target) and os.path.normcase(target) != os.path.normcase(info["path"]):
            problems.append("目标文件已存在：%s" % new_name)
        if len(os.path.basename(target)) > 240:
            problems.append("文件名太长：%s" % new_name)
    return problems


def apply_plan(plan, album, write_tags=True, progress=None):
    problems = validate_plan(plan)
    if problems:
        raise ValueError("；".join(problems))

    total = len(plan)
    undo = {"time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "version": APP_VER, "album": album, "files": []}

    for i, (info, new_name) in enumerate(plan):
        old_name = info["name"]
        record = {
            "dir": os.path.dirname(info["path"]), "old": old_name,
            "new": new_name, "ext": info["ext"],
            "tags_before": None, "vendor_before": None,
            "had_comment": None, "wrote_tags": False,
        }
        if write_tags:
            tags = build_tag_map(info, i + 1, total, album)
            try:
                if info["ext"] == ".flac":
                    payload, vendor, had = write_flac_tags(info["path"], tags)
                    record["tags_before"] = (base64.b64encode(payload).decode("ascii")
                                             if payload is not None else None)
                    record["vendor_before"] = base64.b64encode(vendor or b"").decode("ascii")
                    record["had_comment"] = bool(had)
                else:
                    before = info.get("tags") or {}
                    write_tags_ffmpeg(info["path"], tags)
                    record["tags_before"] = dict(before)
                record["wrote_tags"] = True
            except Exception as exc:
                write_log("写标签失败 %s: %s" % (info["path"], exc))
                record["tag_error"] = str(exc)

        undo["files"].append(record)
        if progress:
            progress(i + 1, total, "写标签 %s" % old_name)

    temp_pairs = []
    try:
        for i, (info, new_name) in enumerate(plan):
            src = info["path"]
            tmp = os.path.join(os.path.dirname(src), ".awb-tmp-%d-%03d" % (os.getpid(), i))
            os.rename(src, tmp)
            temp_pairs.append((tmp, os.path.join(os.path.dirname(src), new_name)))
        for tmp, dst in temp_pairs:
            os.rename(tmp, dst)
    except Exception:
        for tmp, dst in temp_pairs:
            try:
                if os.path.exists(tmp):
                    os.rename(tmp, os.path.join(os.path.dirname(dst), os.path.basename(tmp)))
            except Exception:
                pass
        write_log("改名失败:\n%s" % traceback.format_exc())
        raise

    undo["saved"] = save_json(UNDO_FILE, undo)
    undo["path"] = UNDO_FILE
    if not undo["saved"] and plan:
        fallback = os.path.join(os.path.dirname(plan[0][0]["path"]), UNDO_FALLBACK)
        undo["path"] = fallback
        undo["saved"] = save_json(fallback, undo)
    write_log("已应用 %d 首，专辑=%s" % (total, album))
    return undo


def undo_plan(undo, progress=None):
    files = undo.get("files") or []
    if not files:
        raise ValueError("撤销记录是空的")
    total = len(files)
    temp_pairs = []
    for i, record in enumerate(files):
        folder = record["dir"]
        current = os.path.join(folder, record["new"])
        if not os.path.exists(current):
            raise ValueError("找不到文件：%s" % record["new"])
        tmp = os.path.join(folder, ".awb-undo-%d-%03d" % (os.getpid(), i))
        os.rename(current, tmp)
        temp_pairs.append((tmp, os.path.join(folder, record["old"]), record))
    for tmp, dst, record in temp_pairs:
        os.rename(tmp, dst)

    for i, record in enumerate(files):
        if not record.get("wrote_tags"):
            continue
        path = os.path.join(record["dir"], record["old"])
        try:
            if record["ext"] == ".flac":
                payload = (base64.b64decode(record["tags_before"])
                           if record.get("tags_before") else None)
                restore_flac_comment(path, payload, record.get("had_comment"))
            else:
                before = record.get("tags_before") or {}
                write_tags_ffmpeg(path, before if isinstance(before, dict) else {})
        except Exception:
            write_log("还原标签失败 %s:\n%s" % (path, traceback.format_exc()))
        if progress:
            progress(i + 1, total, "还原 %s" % record["old"])
    return total
