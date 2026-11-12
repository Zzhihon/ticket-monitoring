# v1.7.2 · 2026-11-15 · 安全：升级包解压只走一个安全入口（社区 PR #17）

> 感谢 [@Shengcun-Scanner](https://github.com/Shengcun-Scanner)（Roni Saguey）报告并提交
> 修复：升级包解压可能写到缓存目录之外。

## 报告说的和实测的

报告指出 `core/update.py::apply()` 用 `z.extractall(tmp)` 解开**从 release 下载的升级包**，
成员名由发布方决定，于是 `../x` 或 `/x` 能写到缓存目录外。按"先量再改"的规矩，我们先实测
（Python 3.11 / Windows）：

| 成员名 | 落盘位置 | 结论 |
|---|---|---|
| `../escaped.txt` | `cache/escaped.txt` | **没逃逸**：CPython 的 `_extract_member` 会去掉 `..` |
| `/abs.txt` | `cache/abs.txt` | 没逃逸：前导分隔符被去掉 |
| `C:/evil.txt` | `cache/evil.txt` | 没逃逸：盘符被去掉 |
| `link -> /outside` + `link/pwned.txt` | 平台相关 | **真向量**：`extractall` 不管软链 |

也就是说方向是对的，但**现代 CPython 已把 `..`/绝对路径关进来**；真正能逃出去的是
**符号链接穿透**——而本项目正好发行 Linux/macOS 包（`os.symlink` 无需特权），必须堵死。

## 我们做了什么（在报告基础上加固）

- 新增 `core/safezip.py`：**唯一**解压入口 `safe_extract()`，先全量校验再落盘
  （任一成员不安全 → 一个字节都不写）；拒绝空名 / 绝对路径 / 盘符 / UNC /
  任何 `..` 分量 / 解析后越界 / **符号链接** / fifo / 设备节点；目标处已有软链也不顺着写；
  成员数上限 20 万，解压后体积上限可选（默认不限，避免误伤正常大包）。
- `core/update.py::apply()` 接入它；保留薄别名 `_safe_extract()`，**贡献者自带的 5 条测试
  原样继续跑通**（署名保留在提交里）。
- 新增门禁 `tests/test_r1903_safezip_matrix.py`（19 条）：7 类越界名、软链成员、
  fifo、软链穿透（POSIX）、目标已有软链（POSIX）、正常包仍可解、成员/体积上限、
  **全仓不许再出现裸 `extractall(`**（AST 扫描）、安全文档单源。
- 安全文档收口：并入根 `SECURITY.md` 的「不可信输入」一节，**安全文档只保留这一份**。

## 升级

升级包完整性仍以 SHA256 + 备份回滚为准；解压防护是"包本身恶意"的兜底。
现有配置与数据无需改动。
