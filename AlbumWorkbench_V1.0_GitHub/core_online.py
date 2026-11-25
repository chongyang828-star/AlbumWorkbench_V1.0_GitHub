# -*- coding: utf-8 -*-
"""联网辅助模块：QQ音乐专辑简介/发行日期/封面、iTunes 高清封面兜底、智能推荐 6 个 B 站标签。"""

import concurrent.futures
import html
import json
import os
import re
import urllib.parse
import urllib.request

from core_audio import norm_text

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _fixed_proxies():
    """修复 Windows 系统代理在 Python urllib 中 https 被写成 https://127.0.0.1:xxxx 导致报错的 Bug。"""
    raw = urllib.request.getproxies() or {}
    fixed = {}
    for k, v in raw.items():
        if not v:
            continue
        if k.lower() == "https" and v.lower().startswith("https://"):
            v = "http://" + v[8:]
        fixed[k] = v
    return fixed


def _open_url(req, timeout=8, prefer_direct=True):
    """智能请求：国内源优先直连、失败走修复后的系统代理；海外源优先修复后的系统代理、失败直连。"""
    direct_op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    proxies = _fixed_proxies()
    proxy_op = urllib.request.build_opener(urllib.request.ProxyHandler(proxies)) if proxies else None

    order = ([direct_op, proxy_op] if prefer_direct else [proxy_op, direct_op])
    last_err = None
    for op in order:
        if op is None:
            continue
        try:
            return op.open(req, timeout=timeout)
        except Exception as exc:
            last_err = exc
    if last_err:
        raise last_err
    return urllib.request.urlopen(req, timeout=timeout)


def _http_get_json(url, headers=None, timeout=8, prefer_direct=True):
    req_headers = {"User-Agent": _UA}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    with _open_url(req, timeout=timeout, prefer_direct=prefer_direct) as resp:
        raw = resp.read()
    text = raw.decode("utf-8", "replace").strip()
    # 兼容 JSONP callback(...)
    if text and not text.startswith("{") and "(" in text and text.endswith(")"):
        text = text[text.find("(") + 1:-1]
    return json.loads(text)


def _http_post_json(url, payload_dict, headers=None, timeout=8, prefer_direct=True):
    req_headers = {"User-Agent": _UA, "Content-Type": "application/json;charset=utf-8"}
    if headers:
        req_headers.update(headers)
    data = json.dumps(payload_dict, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=req_headers, method="POST")
    with _open_url(req, timeout=timeout, prefer_direct=prefer_direct) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8", "replace"))


def _clean_intro(text):
    if not text:
        return ""
    s = html.unescape(str(text))
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t\u3000]+", " ", ln).strip() for ln in s.split("\n")]
    out = []
    for ln in lines:
        if ln or (out and out[-1] != ""):
            out.append(ln)
    return "\n".join(out).strip()


def _pick_best_match(items, album, artist, get_name, get_artist):
    na = norm_text(album)
    ns = norm_text(artist)
    best, best_sc = None, -1.0
    for it in items:
        n1 = norm_text(get_name(it))
        n2 = norm_text(get_artist(it))
        sc = 0.0
        if na and n1:
            if na == n1:
                sc += 3.0
            elif na in n1 or n1 in na:
                sc += 2.0
        if ns and n2:
            if ns == n2:
                sc += 2.5
            elif ns in n2 or n2 in ns:
                sc += 1.5
        if sc > best_sc:
            best_sc = sc
            best = it
    return best or (items[0] if items else None)


def _search_queries(album, artist):
    """生成由精确到宽松的查询词列表（自动去掉 Explicit/Deluxe 等干扰后缀）。"""
    clean_alb = re.sub(
        r"\s*[\(\[（【](?:Explicit|Deluxe|Remastered|Edition|Standard|Clean)[^\)\]）】]*[\)\]）】]",
        "", album or "", flags=re.I
    ).strip() or (album or "").strip()
    art = (artist or "").strip()
    cands = []
    for q in (("%s %s" % (clean_alb, art)).strip(), clean_alb, ("%s %s" % (album or "", art)).strip()):
        if q and q not in cands:
            cands.append(q)
    return cands


# --------------------------------------------------------------------- QQ 音乐查询
def query_qqmusic_album(album, artist=""):
    """尝试从 QQ 音乐公开接口抓取专辑 ID、MID、链接、简介、发行日期、流派和封面 URL。"""
    queries = _search_queries(album, artist)
    if not queries:
        return {}

    qq_headers = {
        "Referer": "https://y.qq.com/",
        "Origin": "https://y.qq.com",
    }

    album_mid = ""
    album_id = ""
    found_date = ""
    found_singer = ""
    found_name = ""

    # 方案 1：musicu.fcg 搜索专辑 (search_type=2)，先搜 专辑+歌手，搜不到自动降级只搜专辑名
    for query in queries:
        try:
            payload = {
                "comm": {"ct": "19", "cv": "1859", "uin": "0"},
                "req": {
                    "method": "DoSearchForQQMusicDesktop",
                    "module": "music.search.SearchCgiService",
                    "param": {
                        "grp": 1,
                        "num_per_page": 8,
                        "page_num": 1,
                        "query": query,
                        "search_type": 2,
                    },
                },
            }
            res = _http_post_json("http://u.y.qq.com/cgi-bin/musicu.fcg", payload, headers=qq_headers, prefer_direct=True)
            body = ((res.get("req") or {}).get("data") or {}).get("body") or {}
            items = (body.get("album") or {}).get("list") or []
            if items:
                best = _pick_best_match(
                    items, album, artist,
                    lambda x: x.get("albumName") or "",
                    lambda x: x.get("singerName") or "",
                )
                if best:
                    album_mid = best.get("albumMID") or ""
                    album_id = str(best.get("albumID") or "").strip()
                    found_date = best.get("publicTime") or ""
                    found_singer = best.get("singerName") or ""
                    found_name = best.get("albumName") or ""
                    break
        except Exception:
            pass

    # 方案 2：旧搜索接口兜底（使用 http 避免直连 TLS 握手失败）
    if not album_mid:
        for query in queries:
            try:
                url = ("http://c.y.qq.com/soso/fcgi-bin/client_search_cp?"
                       "p=1&n=8&t=8&format=json&w=%s" % urllib.parse.quote(query))
                res = _http_get_json(url, headers=qq_headers, prefer_direct=True)
                items = ((res.get("data") or {}).get("album") or {}).get("list") or []
                if items:
                    best = _pick_best_match(
                        items, album, artist,
                        lambda x: x.get("albumName") or "",
                        lambda x: x.get("singerName") or "",
                    )
                    if best:
                        album_mid = best.get("albumMID") or ""
                        album_id = str(best.get("albumID") or "").strip()
                        found_date = best.get("publicTime") or ""
                        found_singer = best.get("singerName") or ""
                        found_name = best.get("albumName") or ""
                        break
            except Exception:
                pass

    if not album_mid:
        return {}

    result = {
        "source": "QQ音乐",
        "album_id": album_id,
        "album_mid": album_mid,
        "album_url": "https://y.qq.com/n/ryqq/albumDetail/%s" % album_mid,
        "album": found_name,
        "artist": found_singer,
        "date": found_date,
        "genre": "",
        "intro": "",
        "cover_url": "https://y.gtimg.cn/music/photo_new/T002R800x800M000%s.jpg" % album_mid,
    }

    # 拉取专辑详情（含 QQ 音乐简介 desc、发行日期 aDate、流派 genre）
    try:
        detail_url = ("http://c.y.qq.com/v8/fcg-bin/fcg_v8_album_info_cp.fcg?"
                      "albummid=%s&format=json" % urllib.parse.quote(album_mid))
        det = _http_get_json(detail_url, headers=qq_headers, prefer_direct=True)
        data = det.get("data") or {}
        if data.get("desc"):
            result["intro"] = _clean_intro(data.get("desc"))
        if data.get("aDate"):
            result["date"] = str(data.get("aDate")).strip()[:10]
        if data.get("genre"):
            result["genre"] = str(data.get("genre")).strip()
        if data.get("singername") and not result["artist"]:
            result["artist"] = str(data.get("singername")).strip()
    except Exception:
        pass

    return result


def _norm_date(val):
    """把各种日期格式归一化为 YYYY-MM-DD，失败返回空字符串。"""
    if not val:
        return ""
    s = str(val).strip()
    m = re.match(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", s)
    if m:
        return "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if re.match(r"^\d{4}$", s):
        return s
    return ""


def _epoch_ms_to_date(ms):
    try:
        import datetime
        ts = int(ms) / 1000.0
        if ts <= 0:
            return ""
        # 东八区日期，避免 UTC 偏移导致差一天
        dt = datetime.datetime.utcfromtimestamp(ts) + datetime.timedelta(hours=8)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return ""


def query_netease_album(album, artist=""):
    """网易云音乐公开搜索兜底：精准拿华语/欧美专辑发行日期 (YYYY-MM-DD) 与封面。"""
    queries = _search_queries(album, artist)
    if not queries:
        return {}
    best = None
    for query in queries:
        try:
            url = ("https://music.163.com/api/search/get/web?s=%s&type=10&limit=6"
                   % urllib.parse.quote(query))
            res = _http_get_json(url, headers={"Referer": "https://music.163.com/"}, timeout=8)
            items = ((res.get("result") or {}).get("albums") or [])
            if items:
                best = _pick_best_match(
                    items, album, artist,
                    lambda x: x.get("name") or "",
                    lambda x: ((x.get("artist") or {}).get("name") or ""),
                )
                if best:
                    break
        except Exception:
            continue
    if not best:
        return {}
    try:
        rdate = _epoch_ms_to_date(best.get("publishTime"))
        aid = str(best.get("id") or "").strip()
        hot_songs = []
        ne_intro = ""
        if aid:
            try:
                det_url = "https://music.163.com/api/album/%s" % urllib.parse.quote(aid)
                det = _http_get_json(det_url, headers={"Referer": "https://music.163.com/"}, timeout=8)
                alb_obj = det.get("album") or {}
                if alb_obj.get("description"):
                    ne_intro = _clean_intro(alb_obj.get("description"))
                songs = det.get("songs") or alb_obj.get("songs") or []
                songs_sorted = sorted(
                    songs,
                    key=lambda s: float(s.get("popularity") or s.get("pop") or 0),
                    reverse=True,
                )
                for s in songs_sorted:
                    sname = (s.get("name") or "").strip()
                    if sname and sname not in hot_songs:
                        hot_songs.append(sname)
            except Exception:
                pass
        return {
            "source": "网易云",
            "album_id": aid,
            "album_url": ("https://music.163.com/album?id=%s" % aid) if aid else "",
            "album": best.get("name") or "",
            "artist": ((best.get("artist") or {}).get("name") or ""),
            "date": rdate,
            "genre": "",
            "intro": ne_intro,
            "hot_songs": hot_songs,
            "cover_url": best.get("picUrl") or "",
        }
    except Exception:
        return {}


# --------------------------------------------------------------------- iTunes 兜底查询
def query_itunes_album(album, artist=""):
    """通过 iTunes Search API 查询发行日期、流派与 1200x1200 高清封面 URL。"""
    query = ("%s %s" % (album, artist)).strip()
    if not query:
        return {}

    has_zh = bool(re.search(r"[\u4e00-\u9fff]", query))
    countries = ("cn", "us") if has_zh else ("us", "cn")

    for country in countries:
        try:
            url = ("https://itunes.apple.com/search?term=%s&entity=album&limit=6&country=%s"
                   % (urllib.parse.quote(query), country))
            res = _http_get_json(url, timeout=8, prefer_direct=False)
            items = res.get("results") or []
            if not items:
                continue
            best = _pick_best_match(
                items, album, artist,
                lambda x: x.get("collectionName") or "",
                lambda x: x.get("artistName") or "",
            )
            if not best:
                continue
            rdate = _norm_date((best.get("releaseDate") or "")[:10])
            art = best.get("artworkUrl100") or best.get("artworkUrl60") or ""
            if art:
                art = re.sub(r"\d+x\d+bb", "1200x1200bb", art)
            return {
                "source": "iTunes",
                "album": best.get("collectionName") or "",
                "artist": best.get("artistName") or "",
                "date": rdate,
                "genre": best.get("primaryGenreName") or "",
                "intro": "",
                "cover_url": art,
            }
        except Exception:
            continue
    return {}


def fetch_album_metadata(album, artist=""):
    """并发查询 QQ 音乐 + 网易云 + iTunes，一次拿到日期、双平台专辑链接、封面、QQ简介。"""
    qq, ne, it = {}, {}, {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        f_qq = pool.submit(query_qqmusic_album, album, artist)
        f_ne = pool.submit(query_netease_album, album, artist)
        f_it = pool.submit(query_itunes_album, album, artist)
        for fut, name in ((f_qq, "qq"), (f_ne, "ne"), (f_it, "it")):
            try:
                res = fut.result(timeout=10) or {}
            except Exception:
                res = {}
            if name == "qq":
                qq = res
            elif name == "ne":
                ne = res
            else:
                it = res

    date_val = _norm_date(qq.get("date")) or _norm_date(ne.get("date")) or _norm_date(it.get("date")) or ""
    genre_val = qq.get("genre") or it.get("genre") or ""
    intro_val = qq.get("intro") or ne.get("intro") or ""
    cover_urls = [u for u in (it.get("cover_url"), qq.get("cover_url"), ne.get("cover_url")) if u]

    return {
        "album": album or qq.get("album") or ne.get("album") or it.get("album") or "",
        "artist": artist or qq.get("artist") or ne.get("artist") or it.get("artist") or "",
        "date": date_val,
        "genre": genre_val,
        "intro": intro_val,
        "hot_songs": ne.get("hot_songs") or [],
        "qq_id": qq.get("album_id") or "",
        "qq_url": qq.get("album_url") or "",
        "qq_mid": qq.get("album_mid") or "",
        "netease_url": ne.get("album_url") or "",
        "netease_id": ne.get("album_id") or "",
        "cover_urls": cover_urls,
        "sources": [s for s in (qq.get("source"), ne.get("source"), it.get("source")) if s],
    }


def download_cover_image(urls, dest_folder, filename="cover.jpg"):
    """按顺序尝试下载高清封面到 dest_folder，返回保存路径或 None。"""
    for url in urls or []:
        try:
            headers = {"User-Agent": _UA}
            if "gtimg.cn" in url or "qq.com" in url:
                headers["Referer"] = "https://y.qq.com/"
            req = urllib.request.Request(url, headers=headers)
            prefer_direct = ("gtimg.cn" in url or "qq.com" in url or "126.net" in url or "163.com" in url)
            with _open_url(req, timeout=10, prefer_direct=prefer_direct) as resp:
                data = resp.read()
                ctype = (resp.headers.get("Content-Type") or "").lower()
            if len(data) < 1024:
                continue
            ext = ".png" if ("png" in ctype or data[:8] == b"\x89PNG\r\n\x1a\n") else ".jpg"
            stem = os.path.splitext(filename)[0]
            out_path = os.path.join(dest_folder, stem + ext)
            with open(out_path, "wb") as f:
                f.write(data)
            return out_path
        except Exception:
            continue
    return None


# --------------------------------------------------------------------- 专辑强相关推荐标签（热门歌名 + 简介关键词）
_INTRO_DOMAIN_KEYWORDS = (
    "中国风", "古风", "概念专辑", "R&B", "节奏布鲁斯", "灵魂乐", "另类R&B",
    "嘻哈", "说唱", "Hip-Hop", "摇滚", "英伦摇滚", "迷幻摇滚", "独立摇滚",
    "后摇", "朋克", "民谣", "独立民谣", "爵士", "电子音乐", "合成器",
    "纯音乐", "古典", "影视原声", "OST", "粤语", "港乐", "J-POP", "K-POP",
    "金曲奖", "格莱美", "全创作", "词曲创作", "黑色幽默", "先锋", "实验音乐",
    "布鲁斯", "蓝调", "雷鬼", "乡村音乐", "金属", "动漫", "ACG", "国风",
    "电子", "独立音乐", "另类音乐", "新世纪", "氛围音乐", "City Pop",
)

_CLEAN_TITLE_PAREN = re.compile(
    r"\s*[\(\[（【](?:Explicit|Deluxe|Remastered|Edition|Live|feat\.?[^）\)\]]*|口白|伴奏|纯音乐|Inst\.?)[^\)\]）】]*[\)\]）】]",
    re.I,
)


def _clean_tag_title(text):
    s = _CLEAN_TITLE_PAREN.sub("", (text or "").strip()).strip()
    return re.sub(r"\s+", " ", s)


def _extract_intro_keywords(intro, track_titles):
    """从专辑简介中提取：1) 被点名提到的歌曲名；2) 书名号/引号专有概念词；3) 真实命中的风格关键词。"""
    if not intro:
        return [], []
    mentioned_tracks = []
    for title in track_titles:
        clean_t = _clean_tag_title(title)
        if len(clean_t) >= 2 and clean_t.lower() in intro.lower():
            if clean_t not in mentioned_tracks:
                mentioned_tracks.append(clean_t)

    keywords = []
    # 提取简介中 《...》 / 「...」 / “...” 里的短概念词（2~10字）
    for m in re.finditer(r"[《「“]([^》」”\r\n]{2,10})[》」”]", intro):
        word = m.group(1).strip()
        if not word or re.search(r"[，。！？、：；,.!?:;]", word):
            continue
        if word not in keywords:
            keywords.append(word)

    # 匹配简介中真实出现的风格/领域词
    intro_low = intro.lower()
    for kw in _INTRO_DOMAIN_KEYWORDS:
        if kw.lower() in intro_low and kw not in keywords:
            keywords.append(kw)

    return mentioned_tracks, keywords


def recommend_album_tags(album="", artist="", date_str="", genre="",
                         tracks=None, intro="", hot_songs=None):
    """返回强关联该专辑的候选标签池（前 6 个最贴合：歌手 + 专辑 + 热门歌名 + 简介关键词）。"""
    pool = []

    def add(tag):
        t = re.sub(r"\s+", " ", (tag or "").strip())
        if not t or len(t) > 20:
            return
        if t.lower() not in [x.lower() for x in pool]:
            pool.append(t)

    # 1. 歌手 + 专辑名（最核心 2 个）
    if artist:
        main_artist = re.split(r"[/、,&]", artist)[0].strip()
        add(main_artist)
    if album:
        add(_clean_tag_title(album) or album)

    # 整理本地曲目名列表
    local_titles = []
    for t in (tracks or []):
        ct = _clean_tag_title(t.get("title") or t.get("stem") or "")
        if ct and ct not in local_titles:
            local_titles.append(ct)

    # 从简介提取：点名提到的主打歌 + 简介关键词
    intro_tracks, intro_kws = _extract_intro_keywords(intro or "", local_titles)

    # 2. 热门歌曲名（优先：网易云热度排序 > 简介点名的歌 > 专辑前几首歌）
    ranked_songs = []
    for s in (hot_songs or []):
        cs = _clean_tag_title(s)
        if cs and cs.lower() not in [x.lower() for x in ranked_songs]:
            ranked_songs.append(cs)
    for s in intro_tracks:
        if s.lower() not in [x.lower() for x in ranked_songs]:
            ranked_songs.append(s)
    for s in local_titles:
        if s.lower() not in [x.lower() for x in ranked_songs]:
            ranked_songs.append(s)

    # 前 6 个槽位：歌手(1) + 专辑(1) + 最火的 3 首代表歌 + 1 个简介关键词（没有简介关键词就补第 4 首热门歌）
    for s in ranked_songs[:3]:
        add(s)

    if intro_kws:
        add(intro_kws[0])
    elif len(ranked_songs) > 3:
        add(ranked_songs[3])

    # 3. 剩余槽位（供「换一批」）：其余简介关键词 + 专辑其余歌曲名 + 发行年份
    for kw in intro_kws[1:]:
        add(kw)
    for s in ranked_songs[3:]:
        add(s)

    m_yr = re.match(r"(\d{4})", date_str or "")
    if m_yr:
        add("%s年" % m_yr.group(1))
    if genre and genre.strip() not in ("Pop", "流行"):
        add(genre.strip())

    return pool
