"""表情包入库事务：去重、最多 100 张、按入库顺序替换旧图。"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading

import sticker_lib as S

MAX_STICKERS = 100
_lock = threading.RLock()


@contextmanager
def library_lock():
    """与控制台删除操作共享跨进程锁。"""
    with _lock:
        p = Path(S.HERE) / ".sticker-library.lock"
        with p.open("a+b") as f:
            if os.name == "nt":
                import msvcrt
                if p.stat().st_size == 0:
                    f.write(b"0"); f.flush()
                f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(f, fcntl.LOCK_UN)


def atomic_bytes(path, body):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name, suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def write_json(path, value):
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def image_files():
    lib = Path(S.LIB)
    lib.mkdir(parents=True, exist_ok=True)
    files = []
    for p in lib.iterdir():
        if p.suffix.lower() not in S.EXTS:
            continue
        if p.is_symlink():
            raise ValueError("图库中存在图片符号链接，请先移除链接")
        if p.is_file():
            files.append(p)
    return files


def inventory():
    p = Path(S.HERE) / "sticker_inventory.json"
    value = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"items": {}, "next_order": 1, "source_history": []}
    rows = S.entries()
    files = {p.name for p in image_files()}
    # 旧图库没有入库时间，用原索引顺序初始化；之后的新增均记录单调顺序。
    ordered = list(dict.fromkeys([r["file"] for r in rows] + sorted(files)))
    try:
        legacy = json.loads((Path(S.LIB) / "sources-chinesebqb.json").read_text(encoding="utf-8-sig"))
        sources = {r["file"]: r.get("path") for r in legacy.get("files", [])}
    except FileNotFoundError:
        sources = {}
    for name in ordered:
        if name in files and name not in value["items"]:
            value["items"][name] = {"order": value["next_order"], "source_path": sources.get(name)}
            value["next_order"] += 1
    value["items"] = {name: row for name, row in value["items"].items() if name in files}
    return value


def _index_bytes(rows):
    path = Path(S.INDEX)
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    table = [i for i, line in enumerate(lines) if line.lstrip().startswith("|")]
    prefix = lines[:table[0]] if table else ["# 表情包索引", ""]
    suffix = lines[table[-1] + 1:] if table else []
    import re
    prefix = [re.sub(r"当前共\s*\d+\s*张", "当前共 %d 张" % len(rows), line) for line in prefix]
    clean = lambda x: str(x or "").replace("|", "／").replace("\n", " ").replace("\r", " ")[:160]
    body = prefix + ["| 文件 | 画面内容 | 适用场景 | 关键词 |", "|---|---|---|---|"]
    body += ["| %s | %s | %s | %s |" % tuple(clean(r.get(k)) for k in ("file", "what", "scene", "keys")) for r in rows]
    return ("\n".join(body + suffix) + "\n").encode()


def record_sent_image(path, account, room, server_id, digest):
    """只登记已获服务器编号且内容未变的图库图片，供引用精确定位。"""
    path = Path(path)
    if not server_id or path.resolve().parent != Path(S.LIB).resolve():
        return
    with library_lock():
        if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            return
        registry = Path(S.HERE) / "sticker_sent_messages.json"
        records = json.loads(registry.read_text(encoding="utf-8")) if registry.exists() else {}
        key = json.dumps([account, room, str(server_id)], ensure_ascii=False)
        records[key] = {"file": path.name, "sha256": digest}
        write_json(registry, records)


def quoted_image(account, room, reference, media_path=None):
    """按发送记录或引用原图内容定位；不用文字描述或相似图猜测。"""
    if reference.get("state") == "recalled" or str(reference.get("type")) not in ("3", "47"):
        raise ValueError("引用的消息不是可用图片或表情包，未删除")
    registry = Path(S.HERE) / "sticker_sent_messages.json"
    records = json.loads(registry.read_text(encoding="utf-8")) if registry.exists() else {}
    sender = reference.get("senderId")
    own = sender == account or (not sender and reference.get("direction") == "outgoing")
    key = json.dumps([account, room, str(reference.get("serverId") or "")], ensure_ascii=False)
    row = records.get(key) if own else None
    files = image_files()
    if row:
        target = next((p for p in files if p.name == row["file"]), None)
        if target is None or hashlib.sha256(target.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("引用的表情包已删除或被替换，未删除其他图片")
        return row["file"], row["sha256"]
    if not media_path:
        return None
    digest = hashlib.sha256(Path(media_path).read_bytes()).hexdigest()
    matches = []
    for path in files:
        original = hashlib.sha256(path.read_bytes()).hexdigest()
        matched = original == digest
        # 原生发送会生成 JPEG 副本；历史消息可按上传副本的完整内容匹配。
        if not matched and path.suffix.lower() != ".gif":
            import native_wechat
            try:
                matched = any(hashlib.sha256(data).hexdigest() == digest
                              for data in native_wechat.image_variants(path))
            except (OSError, ValueError):
                continue
        if matched:
            matches.append((path.name, original))
    if len(matches) != 1:
        raise ValueError("无法唯一确定引用的表情包在图库中的文件，未删除")
    return matches[0]


def delete_images(scope, names, actor, is_admin, request_id, expected_hashes=None):
    """按已核验范围删除；图片、索引、去重缓存和回执共同提交或回滚。"""
    if scope not in ("files", "animated", "all", "children"):
        raise ValueError("不支持的删除范围")
    with library_lock():
        if not is_admin(actor):
            raise PermissionError("管理员权限已失效，未删除")
        receipt_path = Path(S.HERE) / "sticker_delete_results.json"
        receipts = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.exists() else {}
        if request_id in receipts:
            return receipts[request_id]
        lib = Path(S.LIB).resolve()
        files = image_files()
        by_name = {p.name: p for p in files}
        if scope == "files":
            if not names or any(n not in by_name for n in names):
                raise ValueError("指定表情包不存在，未删除任何文件")
            victims = [by_name[n] for n in dict.fromkeys(names)]
            if expected_hashes and any(hashlib.sha256(p.read_bytes()).hexdigest() != expected_hashes.get(p.name)
                                       for p in victims):
                raise ValueError("引用的表情包内容已变化，未删除")
        elif scope == "all":
            victims = files
        elif scope == "children":
            # 只按实际画面描述判断；配文、适用场景、关键词中的“宝宝”等不代表小孩形象。
            import re
            child = r"(?:小孩(?:子)?|儿童|孩童|孩子|婴儿|幼儿|萌娃|小男孩|小女孩|男童|女童)"
            selected = set()
            for row in S.entries():
                appearance = re.split(r"画面/文字|文字|字幕|配文|配有|下方|[「『“\"]", row.get("what", ""), maxsplit=1)[0]
                appearance = re.sub(r"(?:不是|并非|没有|不含|非)(?:一个|一名)?" + child, "", appearance)
                if re.search(child, appearance):
                    selected.add(row["file"])
            victims = [p for p in files if p.name in selected]
        else:
            from PIL import Image
            victims = []
            for p in files:
                # GIF 按图库动图类别处理；同时识别其他格式的多帧图片。
                if p.suffix.lower() == ".gif":
                    victims.append(p)
                    continue
                with Image.open(p) as image:
                    if getattr(image, "is_animated", False):
                        victims.append(p)
        for p in victims:
            if p.resolve().parent != lib:
                raise ValueError("删除目标不在表情包库内")
        removed = {p.name for p in victims}
        meta = inventory()
        for name in removed:
            meta["items"].pop(name, None)
        rows = [r for r in S.entries() if r["file"] not in removed]
        for p in files:
            if p.name not in removed and not any(r["file"] == p.name for r in rows):
                rows.append({"file": p.name, "what": "原有表情包", "scene": "", "keys": ""})
        hashes = S._load(S.HASHES, {})
        used = S._load(S.USED, {})
        for name in removed:
            hashes.pop(name, None)
            used.pop(name, None)
        result = {"status": "completed", "deleted": len(removed), "files": sorted(removed),
                  "total": len(files) - len(removed)}
        receipts[request_id] = result
        updates = {
            Path(S.INDEX): _index_bytes(rows),
            Path(S.HERE) / "sticker_inventory.json": (json.dumps(meta, ensure_ascii=False, indent=2) + "\n").encode(),
            Path(S.HASHES): (json.dumps(hashes, ensure_ascii=False, indent=2) + "\n").encode(),
            Path(S.USED): (json.dumps(used, ensure_ascii=False, indent=2) + "\n").encode(),
            receipt_path: (json.dumps(receipts, ensure_ascii=False, indent=2) + "\n").encode(),
        }
        before = {p: p.read_bytes() if p.exists() else None for p in updates}
        stage = Path(tempfile.mkdtemp(prefix=".sticker-delete-", dir=lib))
        if stage.resolve().parent != lib:
            raise ValueError("删除暂存目录不在图库内")
        moved = []
        committed = False
        try:
            for p in victims:
                p.replace(stage / p.name)
                moved.append(p)
            for p, body in updates.items():
                atomic_bytes(p, body)
            committed = True
        except Exception:
            for p in moved:
                (stage / p.name).replace(p)
            for p, body in before.items():
                if body is None:
                    p.unlink(missing_ok=True)
                else:
                    atomic_bytes(p, body)
            raise
        finally:
            if committed:
                for p in moved:
                    (stage / p.name).unlink(missing_ok=True)
            if not any(stage.iterdir()):
                stage.rmdir()
        return result


def commit_image(image, review, source_path, source_url, actor, is_admin):
    """必须再次验证管理员；只有通过视觉风格审核的图片可以入库。"""
    if review.get("accepted") is not True or review.get("style") not in ("cute", "abstract"):
        raise ValueError("图片未通过指定风格审核")
    with library_lock():
        if not is_admin(actor):
            raise PermissionError("管理员权限已失效，停止入库")
        from PIL import Image
        with Image.open(image) as im:
            im.verify()
        if S.find_dup(str(image)):
            return None
        lib = Path(S.LIB).resolve()
        files = image_files()
        meta = inventory()
        name = "skill_%s.png" % hashlib.sha256(Path(image).read_bytes()).hexdigest()[:20]
        target = lib / name
        if target.exists():
            return None
        victims = sorted(files, key=lambda p: meta["items"][p.name]["order"])[:max(0, len(files) - MAX_STICKERS + 1)]
        victim_names = {p.name for p in victims}
        rows = {r["file"]: r for r in S.entries() if r["file"] not in victim_names}
        for p in files:
            if p.name not in victim_names:
                rows.setdefault(p.name, {"file": p.name, "what": "原有表情包", "scene": "", "keys": ""})
        rows[name] = {"file": name, "what": review.get("what", ""), "scene": review.get("scene", ""), "keys": review.get("keys", "")}
        for old in victim_names:
            meta["items"].pop(old, None)
        meta["items"][name] = {"order": meta["next_order"], "style": review["style"], "source_path": source_path, "source_url": source_url}
        meta["next_order"] += 1
        meta["source_history"] = list(dict.fromkeys(meta.get("source_history", []) + [source_path]))
        paths = [Path(S.INDEX), Path(S.HERE) / "sticker_inventory.json", Path(S.SAVED), Path(S.HASHES)]
        before = {p: p.read_bytes() if p.exists() else None for p in paths}
        saved = S._load(S.SAVED, {}); saved[S._sha(str(image))] = __import__('time').time()
        hashes = S._load(S.HASHES, {})
        for old in victim_names:
            hashes.pop(old, None)
        hashes[name] = S._dhash(str(image))
        stage = Path(tempfile.mkdtemp(prefix=".sticker-transaction-", dir=lib))
        assert stage.resolve().parent == lib
        moved = []
        try:
            # 先把旧图移出活跃库，再放新图，避免任何时刻突破 100 张。
            for old in victims:
                old.replace(stage / (old.name + ".old")); moved.append(old)
            atomic_bytes(target, Path(image).read_bytes())
            atomic_bytes(paths[0], _index_bytes(list(rows.values())))
            write_json(paths[1], meta)
            write_json(paths[2], saved)
            write_json(paths[3], hashes)
            if len(image_files()) > MAX_STICKERS:
                raise RuntimeError("图库数量校验失败")
        except Exception:
            target.unlink(missing_ok=True)
            for old in moved:
                (stage / (old.name + ".old")).replace(old)
            for p, body in before.items():
                if body is None:
                    p.unlink(missing_ok=True)
                else:
                    atomic_bytes(p, body)
            raise
        finally:
            for old in moved:
                (stage / (old.name + ".old")).unlink(missing_ok=True)
            stage.rmdir()
        return {"file": name, "replaced": sorted(victim_names), "total": len(image_files())}
