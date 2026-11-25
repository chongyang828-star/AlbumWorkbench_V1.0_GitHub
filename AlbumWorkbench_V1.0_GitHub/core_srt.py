# -*- coding: utf-8 -*-
"""核心模块：SRT 编码解析与合并、开头自动插编号歌名、时间轴生成与全局加减秒数。"""

import os
import re

from core_audio import (
    natural_key,
    norm_text,
    core_text,
    strip_track_prefix,
    split_title_artist,
    title_similarity,
)

MODE_AUDIO = "audio"
MODE_APPEND = "append"

_TC_FULL_RE = re.compile(r"(\d{1,3})\s*:\s*(\d{1,2})\s*:\s*(\d{1,2})\s*[,.]\s*(\d{1,3})")
_TC_SHORT_RE = re.compile(r"(\d{1,2})\s*:\s*(\d{1,2})\s*[,.]\s*(\d{1,3})")
_INDEX_RE = re.compile(r"^\s*\d{1,6}\s*$")
_TS_TOKEN_RE = re.compile(r"(?<!\d)(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?!\d)")


def _to_float(val, default=0.0):
    try:
        if val is None:
            return default
        r = float(str(val).strip())
        if r != r or r == float("inf") or r == float("-inf"):
            return default
        return r
    except Exception:
        return default


def decode_text(path):
    with open(str(path), "rb") as f:
        raw = f.read()
    if not raw:
        return "", "UTF-8"
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace").lstrip("\ufeff"), "UTF-8 BOM"
    if raw.startswith(b"\xff\xfe"):
        return raw.decode("utf-16", "replace").lstrip("\ufeff"), "UTF-16 LE"
    if raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16", "replace").lstrip("\ufeff"), "UTF-16 BE"
    for enc, label in (("utf-8", "UTF-8"), ("gb18030", "GBK")):
        try:
            return raw.decode(enc), label
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", "replace"), "UTF-8"


def _tc_side_sec(text):
    value = str(text or "").strip().replace("：", ":").replace("，", ",").replace(".", ",")
    m = _TC_FULL_RE.search(value)
    if m:
        h, mn, s, ms = m.groups()
    else:
        m = _TC_SHORT_RE.search(value)
        if not m:
            return None
        h = "0"
        mn, s, ms = m.groups()
    return max(0.0, int(h) * 3600 + int(mn) * 60 + int(s) + int(ms.ljust(3, "0")[:3]) / 1000.0)


def _time_line_secs(line):
    if not line or "-->" not in line:
        return None
    left, right = line.split("-->", 1)
    s, e = _tc_side_sec(left), _tc_side_sec(right)
    if s is None or e is None:
        return None
    return s, e


def parse_srt(path):
    try:
        text, enc = decode_text(path)
    except Exception:
        return [], "读取失败"
    cues = []
    text = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    n, i = len(lines), 0
    while i < n:
        secs = _time_line_secs(lines[i])
        if secs is None:
            i += 1
            continue
        start, end = secs
        i += 1
        body = []
        while i < n:
            line = lines[i]
            if not line.strip():
                i += 1
                break
            if _time_line_secs(line) is not None:
                break
            if _INDEX_RE.match(line) and i + 1 < n and _time_line_secs(lines[i + 1]) is not None:
                break
            body.append(line.strip())
            i += 1
        content = "\n".join(body).strip()
        if not content:
            continue
        if end < start:
            start, end = end, start
        cues.append((max(0.0, start), max(0.0, end), content))
    return cues, enc


def sec_to_tc(sec):
    total_ms = max(0, int(round(_to_float(sec, 0.0) * 1000.0)))
    h, rest = divmod(total_ms, 3600000)
    m, rest = divmod(rest, 60000)
    s, ms = divmod(rest, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def fmt_mmss(sec, force_hour=False):
    total = max(0, int(round(_to_float(sec, 0.0))))
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    if h or force_hour:
        return "%02d:%02d:%02d" % (h, m, s)
    return "%02d:%02d" % (m, s)


def fmt_mmss_frames(sec, fps=30, force_hour=False):
    total_frames = max(0, int(round(_to_float(sec, 0.0) * fps)))
    total_sec, frame = divmod(total_frames, fps)
    h, rest = divmod(total_sec, 3600)
    m, s = divmod(rest, 60)
    if h or force_hour:
        return "%02d:%02d:%02d:%02d" % (h, m, s, frame)
    return "%02d:%02d:%02d" % (m, s, frame)


def write_srt(path, cues, bom=True):
    target = os.path.abspath(str(path))
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    blocks = []
    for idx, (start, end, content) in enumerate(cues, 1):
        if end < start:
            start, end = end, start
        body = str(content).replace("\r\n", "\n").replace("\r", "\n").strip("\n").replace("\n", "\r\n")
        blocks.append("%d\r\n%s --> %s\r\n%s" % (idx, sec_to_tc(start), sec_to_tc(end), body))
    data = "\r\n\r\n".join(blocks)
    if data:
        data += "\r\n"
    raw = data.encode("utf-8")
    if bom:
        raw = b"\xef\xbb\xbf" + raw
    with open(target, "wb") as f:
        f.write(raw)


def list_srt_files(folder):
    out = []
    if not folder or not os.path.isdir(folder):
        return out
    for fn in os.listdir(folder):
        if fn.startswith("."):
            continue
        full = os.path.join(folder, fn)
        if os.path.isfile(full) and fn.lower().endswith(".srt"):
            # 排除工具自己导出的合并总字幕
            if "_合并字幕" in fn or "_merged" in fn.lower():
                continue
            out.append(full)
    out.sort(key=lambda p: natural_key(os.path.basename(p)))
    return out


def align_srts_to_tracks(srt_paths, tracks):
    """按已排序的音频列表 tracks 自动对齐 srt_paths。

    返回列表 aligned，与 tracks 一一对应，每项为 {"track": info, "srt": path_or_None, "score": float}，
    以及未能匹配的多余 srt 路径列表 leftovers。
    """
    srt_infos = []
    for p in srt_paths:
        stem = os.path.splitext(os.path.basename(p))[0]
        title, _artist = split_title_artist(stem)
        srt_infos.append({"path": p, "stem": stem, "title": title})

    pairs = []
    for ti, t in enumerate(tracks):
        t_title = t.get("title") or t.get("stem") or ""
        t_stem = strip_track_prefix(t.get("stem") or "")
        for si, s in enumerate(srt_infos):
            sc = max(
                title_similarity(t_title, s["title"]),
                title_similarity(t_stem, s["stem"]),
                title_similarity(t.get("stem", ""), s["stem"]),
            )
            pairs.append((sc, ti, si))
    pairs.sort(reverse=True)

    used_t, used_s, mapping = set(), set(), {}
    for sc, ti, si in pairs:
        if ti in used_t or si in used_s:
            continue
        if sc < 0.45:
            break
        mapping[ti] = (srt_infos[si]["path"], sc)
        used_t.add(ti)
        used_s.add(si)

    aligned = []
    for ti, t in enumerate(tracks):
        srt_p, sc = mapping.get(ti, (None, 0.0))
        aligned.append({"track": t, "srt": srt_p, "score": sc})

    leftovers = [s["path"] for si, s in enumerate(srt_infos) if si not in used_s]
    return aligned, leftovers


def merge_album_srts(rows, mode=MODE_AUDIO, gap_sec=0.0, insert_title_cue=False):
    """按行列表合并 SRT。

    rows: [{"label": "01 - Nikes (Explicit) - Frank Ocean",
            "srt": path_or_None,
            "duration": float_or_None}]
    insert_title_cue: True 时，在每首歌开头到第一句歌词出现前自动插入一条歌名片段。
    返回 (merged_cues, warnings, stats_lines)。
    """
    merged = []
    warnings = []
    stats = []
    acc = 0.0
    gap = max(0.0, _to_float(gap_sec, 0.0))

    for idx, row in enumerate(rows, 1):
        label = row.get("label") or ("第 %02d 首" % idx)
        srt_path = row.get("srt")
        dur = _to_float(row.get("duration"), None) if row.get("duration") else None

        cues = []
        enc = ""
        if srt_path and os.path.isfile(srt_path):
            cues, enc = parse_srt(srt_path)

        if not cues:
            # 无歌词文件或空字幕（纯音乐 / 缺歌词）
            if mode == MODE_AUDIO and dur and dur > 0:
                if insert_title_cue:
                    title_end = acc + min(5.0, max(1.0, dur - 0.1))
                    merged.append((acc, title_end, label))
                    warnings.append("⚠ %s：无歌词，已自动插入 5 秒歌名片段，建议在达芬奇中确认" % label)
                else:
                    warnings.append("⚠ %s：无对应歌词，已按音频时长 %s 留空" % (label, fmt_mmss(dur)))
                acc += dur + gap
                stats.append("%s：无歌词（按音频 %s 占位）" % (label, fmt_mmss(dur)))
            else:
                warnings.append("⚠ %s：无对应歌词且无音频时长，已跳过" % label)
            continue

        first_start = min(c[0] for c in cues)
        last_end = max(c[1] for c in cues)

        if mode == MODE_AUDIO and dur and dur > 0:
            offset = acc
            span = dur
        elif mode == MODE_AUDIO:
            offset = acc
            span = last_end
            warnings.append("⚠ %s：缺少音频时长，改按字幕长度排" % label)
        else:  # MODE_APPEND
            offset = acc - first_start
            span = max(0.0, last_end - first_start)

        # 开头自动插编号歌名（严格落在“歌曲开头 → 第一句歌词前”）
        if insert_title_cue:
            if mode == MODE_AUDIO:
                if first_start >= 0.45:
                    # 留 50ms 间隙避免与第一句歌词时间重叠
                    t_end = offset + max(0.35, first_start - 0.05)
                    merged.append((offset, t_end, label))
                else:
                    warnings.append(
                        "⚠ %s：第一句歌词在 %.2fs 就开始（前奏太短），未自动插歌名，请用置顶歌单条手动补"
                        % (label, first_start)
                    )
            else:
                # 顺序拼接模式无前奏空档，提示用户
                warnings.append("⚠ %s：顺序拼接模式无前奏空档，请改用「按音频时长」模式以自动插歌名" % label)

        for s, e, txt in cues:
            merged.append((max(0.0, s + offset), max(0.0, e + offset), txt))

        stats.append("%s：%d 条（%s）" % (label, len(cues), enc))
        acc += span + gap

    merged.sort(key=lambda item: (item[0], item[1]))
    return merged, warnings, stats


# --------------------------------------------------------------------- 时间轴生成与加减秒
def generate_timeline(tracks, offset_sec=3, include_index=True, include_artist=True,
                      show_frames=False, fps=30):
    """根据排好序的 tracks 自动累加时间轴。

    offset_sec: 全局偏移秒数（如 +3 适配 3 秒片头，或 -3 提前 3 秒）。
    show_frames: False 默认精确到秒 (MM:SS)；True 备用精确到帧 (MM:SS:FF)。
    返回 (timeline_text, missing_dur_names)。
    """
    lines = []
    missing = []
    acc = 0.0
    total_est = sum((_to_float(t.get("dur"), 0.0) for t in tracks), 0.0) + max(0, offset_sec)
    force_hour = total_est >= 3600

    for i, t in enumerate(tracks, 1):
        ts = max(0.0, acc + offset_sec)
        num = "%02d" % i if i < 100 else "%03d" % i
        title = (t.get("title") or t.get("stem") or "").strip()
        artist = (t.get("artist") or "").strip()
        parts = []
        if include_index:
            parts.append(num)
        parts.append(title)
        if include_artist and artist:
            parts.append(artist)
        label = " - ".join(parts) if include_index else (
            ("%s - %s" % (title, artist)) if (include_artist and artist) else title
        )
        tc = (fmt_mmss_frames(ts, fps=fps, force_hour=force_hour)
              if show_frames else fmt_mmss(ts, force_hour=force_hour))
        lines.append("%s %s" % (tc, label))

        dur = _to_float(t.get("dur"), 0.0)
        if dur <= 0:
            missing.append(t.get("name") or label)
        acc += dur

    return "\n".join(lines), missing


def shift_timeline_text(text, delta_sec):
    """对任意粘贴进来的时间轴文本里的所有 MM:SS / HH:MM:SS 统一加减 delta_sec 秒。"""
    delta = int(round(_to_float(delta_sec, 0)))

    def repl(m):
        h_str, m_str, s_str = m.group(1), m.group(2), m.group(3)
        h = int(h_str) if h_str is not None else 0
        mn = int(m_str)
        s = int(s_str)
        if mn >= 60 or s >= 60:
            return m.group(0)
        total = max(0, h * 3600 + mn * 60 + s + delta)
        nh, rem = divmod(total, 3600)
        nmn, ns = divmod(rem, 60)
        if h_str is not None or nh > 0:
            return "%02d:%02d:%02d" % (nh, nmn, ns)
        return "%02d:%02d" % (nmn, ns)

    return _TS_TOKEN_RE.sub(repl, text or "")
