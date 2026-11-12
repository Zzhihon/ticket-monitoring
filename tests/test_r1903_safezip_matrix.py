# -*- coding: utf-8 -*-
"""门禁：不可信压缩包只能走 `core.safezip.safe_extract`（zip slip / 软链穿透）。

来历：社区 PR #17（Shengcun-Scanner）报了升级包解压可能写到缓存目录外。
我们的实测（Python 3.11 / Windows，见 `docs/SECURITY.md`）表明：

- `../x`、`/x`、`C:/x` 在现代 CPython **已被 `_extract_member` 去掉**，落进目标目录；
- 真正的逃逸向量是**符号链接**：成员 `link -> /outside` 先建成软链，随后
  `link/pwned.txt` 顺着它写到外面（Linux/macOS 上 `os.symlink` 无需特权，
  而本项目正好发行这两个平台的包）。

所以既要保留贡献者的判据（`..`/绝对路径显式拒绝），也要把软链这条堵死，
并且**全仓只能有一个解压入口**。
"""
import ast
import io
import os
import stat
import subprocess
import sys
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.safezip import UnsafeArchiveError, is_safe_member, safe_extract  # noqa: E402


def _make_zip(tmp, entries, links=(), fifo=(), big=None):
    p = os.path.join(str(tmp), "pkg.zip")
    with zipfile.ZipFile(p, "w") as z:
        for name, text in entries.items():
            z.writestr(name, text)
        for name, data in links:
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(info, data)
        for name in fifo:
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFIFO | 0o644) << 16
            z.writestr(info, "")
        if big:
            name, size = big
            info = zipfile.ZipInfo(name)
            info.file_size = size
            z.writestr(info, b"\0" * 16)
    return p


def _dest(tmp):
    d = os.path.join(str(tmp), "cache")
    os.makedirs(d, exist_ok=True)
    return d


@pytest.mark.parametrize("bad", [
    "../escaped.txt",
    "a/../../escaped.txt",
    "/abs.txt",
    "C:/evil.txt",
    "C:\\\\evil.txt",
    "//host/share/evil.txt",
    "core/../../evil.txt",
])
def test_traversal_and_absolute_names_are_refused(tmp_path, bad):
    z = _make_zip(tmp_path, {bad: "x", "ok.txt": "fine"})
    dest = _dest(tmp_path)
    with zipfile.ZipFile(z) as f:
        with pytest.raises(UnsafeArchiveError):
            safe_extract(f, dest)
    # 先校验后落盘：一个字节都不许写
    assert os.listdir(dest) == [], os.listdir(dest)


def test_symlink_member_is_refused(tmp_path):
    z = _make_zip(tmp_path, {"ok.txt": "fine"}, links=[("link", "/etc")])
    dest = _dest(tmp_path)
    with zipfile.ZipFile(z) as f:
        with pytest.raises(UnsafeArchiveError):
            safe_extract(f, dest)
    assert os.listdir(dest) == []


def test_fifo_member_is_refused(tmp_path):
    z = _make_zip(tmp_path, {"ok.txt": "fine"}, fifo=["pipe"])
    dest = _dest(tmp_path)
    with zipfile.ZipFile(z) as f:
        with pytest.raises(UnsafeArchiveError):
            safe_extract(f, dest)


@pytest.mark.skipif(os.name != "posix", reason="只有 POSIX 能真正建软链")
def test_symlink_write_through_cannot_escape(tmp_path):
    """`link -> /outside` + `link/pwned.txt`：真能逃出去的那条路，必须被拒。"""
    outside = os.path.join(str(tmp_path), "outside")
    os.makedirs(outside)
    z = _make_zip(tmp_path,
                  {"link/pwned.txt": "pwn"},
                  links=[("link", outside)])
    dest = _dest(tmp_path)
    with zipfile.ZipFile(z) as f:
        with pytest.raises(UnsafeArchiveError):
            safe_extract(f, dest)
    assert not os.path.exists(os.path.join(outside, "pwned.txt"))


@pytest.mark.skipif(os.name != "posix", reason="Windows 建软链需特权")
def test_preexisting_symlink_in_dest_is_not_followed(tmp_path):
    """目标目录里**已有**软链指向外面时，也不许顺着写。"""
    outside = os.path.join(str(tmp_path), "outside")
    os.makedirs(outside)
    dest = _dest(tmp_path)
    os.symlink(outside, os.path.join(dest, "hole"))
    z = _make_zip(tmp_path, {"hole/pwned.txt": "pwn"})
    with zipfile.ZipFile(z) as f:
        with pytest.raises(UnsafeArchiveError):
            safe_extract(f, dest)
    assert not os.path.exists(os.path.join(outside, "pwned.txt"))


def test_normal_package_extracts_with_nested_dirs(tmp_path):
    z = _make_zip(tmp_path, {"core/mod.py": "NEW\n",
                             "nested/deep/x.txt": "D\n"})
    dest = _dest(tmp_path)
    with zipfile.ZipFile(z) as f:
        written = safe_extract(f, dest)
    assert (open(os.path.join(dest, "core", "mod.py"), encoding="utf-8")
            .read()) == "NEW\n"
    assert os.path.exists(os.path.join(dest, "nested", "deep", "x.txt"))
    assert "core/mod.py" in [w.replace(os.sep, "/") for w in written]


def test_member_cap_and_size_cap(tmp_path):
    z = _make_zip(tmp_path, {"a.txt": "1"})
    dest = _dest(tmp_path)
    with zipfile.ZipFile(z) as f:
        with pytest.raises(UnsafeArchiveError):
            safe_extract(f, dest, max_entries=0)
    # writestr() 会用 data 长度覆盖 file_size，所以这里直接卡 1 字节上限
    z2 = _make_zip(tmp_path, {"big.bin": "0123456789"})
    with zipfile.ZipFile(z2) as f:
        with pytest.raises(UnsafeArchiveError):
            safe_extract(f, dest, max_total_bytes=1)


def test_is_safe_member_semantics(tmp_path):
    root = os.path.realpath(str(tmp_path))
    assert is_safe_member("a/b.txt", root)
    assert is_safe_member("a/./b.txt", root)
    assert not is_safe_member("../x", root)
    assert not is_safe_member("a/../../x", root)
    assert not is_safe_member("/etc/passwd", root)
    assert not is_safe_member("C:/x", root)


def test_repo_has_single_extraction_entry():
    """全仓不许再有**真实调用** `.extractall(`——解压只走 core.safezip。

    用 AST 而不是 `git grep`：后者在 GBK 控制台下会把中文注释解崩，
    也会把文档字符串里提到的 `extractall(` 误判成调用。
    """
    hits = []
    skip_dirs = {".git", "_scratch", "_tools", "_vendor", "_internal", "build",
                 "dist", "data", "logs", "user_data", "__pycache__",
                 ".pytest_cache", "_build", "_build_check"}
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
            if rel == "core/safezip.py":
                continue
            try:
                tree = ast.parse(io.open(path, encoding="utf-8").read())
            except (OSError, SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "extractall"):
                    hits.append("%s:%d" % (rel, node.lineno))
    assert not hits, "这些地方还在直接 extractall：%s" % hits[:5]


def test_security_doc_names_the_entry():
    """安全文档只有一份：根 `SECURITY.md`（README 指向它），不许再分叉。"""
    assert not os.path.exists(os.path.join(ROOT, "docs", "SECURITY.md")), \
        "又出现了 docs/SECURITY.md——安全文档必须单源（根 SECURITY.md）"
    p = os.path.join(ROOT, "SECURITY.md")
    assert os.path.exists(p), "缺根 SECURITY.md"
    d = io.open(p, encoding="utf-8").read()
    for k in ("core.safezip", "safe_extract", "符号链接", "升级包", "extractall"):
        assert k in d, "SECURITY.md 缺：" + k
