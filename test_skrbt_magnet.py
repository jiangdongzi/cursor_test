import tempfile
import unittest
from pathlib import Path

from skrbt_magnet import (
    DEFAULT_COOKIE_FILE,
    DEFAULT_LIMIT,
    DEFAULT_SEARCH_WORKERS,
    DEFAULT_WORKERS,
    DetailPage,
    MagnetAppender,
    build_sofs_filter,
    decide_by_size,
    load_cookie_file,
    make_argument_parser,
    magnet_key,
    parse_detail_page,
    parse_search_page,
    parse_size_argument,
    resolve_page_count,
    resolve_sofs,
)


HASH_A = "0123456789ABCDEF0123456789ABCDEF01234567"
HASH_B = "89ABCDEF0123456789ABCDEF0123456789ABCDEF"


class SizeParsingTests(unittest.TestCase):
    def test_parse_size_argument(self) -> None:
        self.assertEqual(parse_size_argument("600M"), 600 * 1024**2)
        self.assertEqual(parse_size_argument("1.5 GB"), int(1.5 * 1024**3))
        self.assertEqual(parse_size_argument("600"), 600 * 1024**2)

    def test_build_sofs_filter(self) -> None:
        self.assertEqual(
            build_sofs_filter(
                parse_size_argument("1G"),
                parse_size_argument("5G"),
            ),
            "gt1gb-lt5gb",
        )
        self.assertEqual(
            build_sofs_filter(
                parse_size_argument("100M"),
                parse_size_argument("500M"),
            ),
            "gt100mb-lt500mb",
        )
        self.assertEqual(
            build_sofs_filter(parse_size_argument("5G"), None),
            "gt5gb",
        )
        self.assertEqual(
            build_sofs_filter(None, parse_size_argument("5G")),
            "lt5gb",
        )
        self.assertEqual(
            build_sofs_filter(parse_size_argument("600M"), None),
            "gt600mb",
        )

    def test_resolve_sofs_from_args(self) -> None:
        parser = make_argument_parser()
        self.assertEqual(
            resolve_sofs(parser.parse_args(["三国演义"])),
            "gt600mb",
        )
        self.assertEqual(
            resolve_sofs(
                parser.parse_args(
                    ["三国演义", "--size-min", "1G", "--size-max", "5G"]
                )
            ),
            "gt1gb-lt5gb",
        )
        self.assertEqual(
            resolve_sofs(
                parser.parse_args(["三国演义", "--sofs", "gt100mb-lt500mb"])
            ),
            "gt100mb-lt500mb",
        )


class HtmlParsingTests(unittest.TestCase):
    def test_parse_search_results(self) -> None:
        document = """
        <ul class="result">
          <li><a class="rrt other" href="/detail/abc">三国演义 1080P</a></li>
          <li>
            <span class="rrmiv">8.5 GB</span>
            <span class="rrmiv">20</span>
            <span class="rrmiv">2026-07-16</span>
          </li>
        </ul>
        """
        results = parse_search_page(document)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].href, "/detail/abc")
        self.assertEqual(results[0].title, "三国演义 1080P")
        self.assertEqual(results[0].total_size, int(8.5 * 1024**3))

    def test_parse_detail_magnet_and_file_rows(self) -> None:
        document = f"""
        <h3>三国演义全集</h3>
        <p><a href="magnet:?xt=urn:btih:{HASH_A}&amp;dn=test">复制</a></p>
        <ul>
          <li>文件大小：5.4 GB</li>
          <li>ep01.mkv <span>550 MB</span></li>
          <li>ep02.mkv <span>580 MB</span></li>
        </ul>
        """
        detail = parse_detail_page(document)
        self.assertEqual(detail.title, "三国演义全集")
        self.assertEqual(len(detail.magnets), 1)
        self.assertIn("&dn=test", detail.magnets[0])

        decision = decide_by_size(
            detail,
            search_total_size=int(5.4 * 1024**3),
            minimum=parse_size_argument("600M"),
            skip_unknown=False,
        )
        self.assertFalse(decision.keep)
        self.assertTrue(decision.known)
        self.assertEqual(len(decision.individual_sizes), 2)

    def test_keep_when_one_file_reaches_threshold(self) -> None:
        document = f"""
        <h3>三国演义</h3>
        <a href="magnet:?xt=urn:btih:{HASH_A}">magnet</a>
        <table>
          <tr><td>ep01.mkv</td><td>580 MB</td></tr>
          <tr><td>ep02.mkv</td><td>700 MB</td></tr>
        </table>
        """
        detail = parse_detail_page(document)
        decision = decide_by_size(
            detail,
            search_total_size=None,
            minimum=parse_size_argument("600M"),
            skip_unknown=False,
        )
        self.assertTrue(decision.keep)
        self.assertTrue(decision.known)

    def test_match_total_size_keeps_episode_pack(self) -> None:
        document = f"""
        <h3>三国演义全集</h3>
        <a href="magnet:?xt=urn:btih:{HASH_A}">magnet</a>
        <ul>
          <li>文件大小：8.2 GB</li>
          <li>ep01.mkv <span>450 MB</span></li>
          <li>ep02.mkv <span>480 MB</span></li>
        </ul>
        """
        detail = parse_detail_page(document)
        by_file = decide_by_size(
            detail,
            search_total_size=int(8.2 * 1024**3),
            minimum=parse_size_argument("600M"),
            skip_unknown=False,
            match_total=False,
        )
        by_total = decide_by_size(
            detail,
            search_total_size=int(8.2 * 1024**3),
            minimum=parse_size_argument("600M"),
            skip_unknown=False,
            match_total=True,
        )
        self.assertFalse(by_file.keep)
        self.assertTrue(by_total.keep)

    def test_unknown_size_policy(self) -> None:
        detail = DetailPage(
            title="unknown",
            magnets=(f"magnet:?xt=urn:btih:{HASH_A}",),
            blocks=(),
        )
        keep = decide_by_size(
            detail,
            search_total_size=None,
            minimum=parse_size_argument("600M"),
            skip_unknown=False,
        )
        skip = decide_by_size(
            detail,
            search_total_size=None,
            minimum=parse_size_argument("600M"),
            skip_unknown=True,
        )
        self.assertTrue(keep.keep)
        self.assertFalse(skip.keep)
        self.assertFalse(keep.known)
        self.assertFalse(skip.known)


class OutputTests(unittest.TestCase):
    def test_append_and_deduplicate_by_infohash(self) -> None:
        first = f"magnet:?xt=urn:btih:{HASH_A}&dn=one"
        same_hash = f"magnet:?xt=urn:btih:{HASH_A.lower()}&dn=two"
        second = f"magnet:?xt=urn:btih:{HASH_B}"

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "magnets.txt"
            output.write_text(first, encoding="utf-8")
            with MagnetAppender(output) as appender:
                self.assertFalse(appender.append(same_hash))
                self.assertTrue(appender.append(second))

            lines = output.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines, [first, second])
            self.assertEqual(magnet_key(lines[0]), magnet_key(same_hash))

    def test_load_netscape_cookie_file_filters_domain(self) -> None:
        content = (
            "# Netscape HTTP Cookie File\n"
            "#HttpOnly_.skrbtso.top\tTRUE\t/\tTRUE\t0\t"
            "cf_clearance\tclearance-value\n"
            ".example.com\tTRUE\t/\tTRUE\t0\tother\tsecret\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookies.txt"
            path.write_text(content, encoding="utf-8")
            cookie = load_cookie_file(path, "skrbtso.top")
        self.assertEqual(cookie, "cf_clearance=clearance-value")

    def test_default_minimum_is_parsed(self) -> None:
        args = make_argument_parser().parse_args(["三国演义"])
        self.assertEqual(args.min_file_size, 0)
        self.assertEqual(args.limit, DEFAULT_LIMIT)
        self.assertEqual(args.cookie_file, DEFAULT_COOKIE_FILE)
        self.assertEqual(args.workers, DEFAULT_WORKERS)
        self.assertEqual(args.search_workers, DEFAULT_SEARCH_WORKERS)
        self.assertEqual(args.retries, 1)
        self.assertEqual(args.delay, 0.0)
        self.assertIsNone(args.pages)

    def test_resolve_page_count(self) -> None:
        self.assertEqual(resolve_page_count(120, None), 14)
        self.assertEqual(resolve_page_count(120, 3), 3)

    def test_concurrent_appender(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        magnets = [
            f"magnet:?xt=urn:btih:{i:040x}" for i in range(1, 81)
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "magnets.txt"
            with MagnetAppender(output) as appender:
                with ThreadPoolExecutor(max_workers=16) as pool:
                    results = list(pool.map(appender.append, magnets * 2))
            self.assertEqual(sum(1 for ok in results if ok), 80)
            lines = output.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 80)
            self.assertEqual(len(set(lines)), 80)


if __name__ == "__main__":
    unittest.main()
