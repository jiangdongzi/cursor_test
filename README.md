# SkrBT magnet 提取工具

这个脚本会：

1. 按关键词请求 SkrBT 搜索页；
2. 从 `a.rrt` 的 `href` 获取每条资源的详情页；
3. 在详情页提取 `magnet:?xt=urn:btih:...`；
4. 解析详情页文件列表。只有至少一个单文件达到阈值时才保留；
5. 将 magnet 一行一条地追加到输出文件，并按 infohash 去重。

只抓取和使用你有权访问、下载的内容，并遵守站点规则。

## 运行

无需安装第三方 Python 包，Python 3.10 及以上即可。

### 1. 准备 Cookie 文件

由于站点有 Cloudflare 验证，先在浏览器中打开站点并完成验证，然后把请求里的 Cookie（`curl -b` 后面那段，不要外层引号）写入项目根目录的 `cookie.txt`：

```text
JSESSIONID=...; cf_clearance=...; ...
```

也支持浏览器导出的 Netscape 格式 `cookies.txt`（可用 `--cookie-file` 指定）。

### 2. 执行搜索

默认会处理 **120 条**搜索结果，使用 **64 线程并发**抓取，从 `cookie.txt` 读 Cookie，并**自动追加写入当前工作目录的 `magnets.txt`**（不必写 `-o`）：

```powershell
python .\skrbt_magnet.py "三国演义"
```

注意：磁力写入的是**你执行命令时所在目录**下的 `magnets.txt`。若在别的文件夹运行，请去那个文件夹查看，或显式指定：

```powershell
python E:\work\cursor_test\skrbt_magnet.py "naughty america" --limit 2000 -o E:\work\cursor_test\magnets.txt
```

若仍被站点限流，可适当降并发或加一点间隔：

```powershell
python .\skrbt_magnet.py "三国演义" --workers 32 --delay 0.05
```

想更快可继续提高并发：

```powershell
python .\skrbt_magnet.py "三国演义" --workers 128
```

默认请求参数与站点搜索一致：

- `sos=relevance`
- `sofs=gt600mb`（可用 `--size-min` / `--size-max` 覆盖）
- `sot=all`
- `soft=all`
- `som=auto`

## 常用参数

站点大小区间（写入 URL 的 `sofs`）：

```powershell
# 1GB ~ 5GB  →  sofs=gt1gb-lt5gb
python .\skrbt_magnet.py "三国演义" --size-min 1G --size-max 5G

# 100MB ~ 500MB  →  sofs=gt100mb-lt500mb
python .\skrbt_magnet.py "三国演义" --size-min 100M --size-max 500M

# 也可以直接传原始 sofs
python .\skrbt_magnet.py "三国演义" --sofs gt1gb-lt5gb
```

改成处理 2000 条，并按“种子总大小”过滤（适合剧集合集）：

```powershell
python .\skrbt_magnet.py "三国演义" --limit 2000 --match-total-size
```

完全关闭本地单文件大小过滤：

```powershell
python .\skrbt_magnet.py "三国演义" --limit 2000 --min-file-size 0
```

指定 Cookie 文件路径：

```powershell
python .\skrbt_magnet.py "三国演义" --cookie-file .\cookie.txt
```

把本地单文件阈值改成 800 MiB：

```powershell
python .\skrbt_magnet.py "三国演义" --min-file-size 800M
```

关闭站点的大小预筛选：

```powershell
python .\skrbt_magnet.py "三国演义" --sofs all
```

详情页无法识别文件列表时，脚本默认保留 magnet。若希望这种情况也跳过：

```powershell
python .\skrbt_magnet.py "三国演义" --skip-unknown-size
```

## Cloudflare 提示

`cf_clearance` 通常与浏览器 User-Agent、IP 和有效期关联。若出现 HTTP 403 或验证页面：

1. 在同一台电脑、同一网络中重新用浏览器访问站点并完成验证；
2. 把最新 Cookie 覆盖写入 `cookie.txt`；
3. 如浏览器 User-Agent 与脚本默认值不同，用 `--user-agent` 传入实际值。

Cookie 相当于临时会话凭据，不要提交到版本库（已在 `.gitignore` 中忽略 `cookie.txt`）。

## 测试

```powershell
python -m unittest -v
```
