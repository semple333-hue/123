#!/usr/bin/env python3
import argparse
import csv
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from typing import Dict, Iterable, List, Optional, Tuple

BASE_URL = "https://iss.moex.com/iss"
DEFAULT_INDEX = "IMOEX"
DEFAULT_BOARD = "TQBR"
DEFAULT_ENGINE = "stock"
DEFAULT_MARKET = "shares"


class MoexError(RuntimeError):
    pass


def fetch_json(url: str, timeout: int = 30, retries: int = 3, backoff: float = 0.8) -> dict:
    headers = {
        "User-Agent": "moex-normalized-pe/1.0 (+https://iss.moex.com)",
        "Accept": "application/json",
    }
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.load(response)
        except Exception as exc:  # pragma: no cover - network errors
            if attempt == retries - 1:
                raise MoexError(f"Failed to fetch {url}: {exc}") from exc
            time.sleep(backoff * (2 ** attempt))
    raise MoexError(f"Failed to fetch {url}")


def chunked(items: List[str], size: int) -> Iterable[List[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def extract_columns(block: dict) -> Tuple[List[str], List[List[object]]]:
    columns = block.get("columns", [])
    data = block.get("data", [])
    return columns, data


def index_constituents(index_code: str) -> List[str]:
    params = urllib.parse.urlencode({"analytics.columns": "SECID"})
    url = f"{BASE_URL}/statistics/engines/stock/markets/index/analytics/{index_code}.json?{params}"
    payload = fetch_json(url)
    columns, data = extract_columns(payload.get("analytics", {}))
    if not columns:
        raise MoexError(f"No analytics columns returned for index {index_code}")
    secid_idx = columns.index("SECID")
    return [row[secid_idx] for row in data if row[secid_idx]]


def parse_block_map(columns: List[str], rows: List[List[object]], key: str) -> Dict[str, Dict[str, object]]:
    if key not in columns:
        return {}
    key_idx = columns.index(key)
    mapping = {}
    for row in rows:
        secid = row[key_idx]
        if not secid:
            continue
        mapping[secid] = {col: row[idx] for idx, col in enumerate(columns)}
    return mapping


def normalized_pe_value(record: Dict[str, object]) -> Optional[object]:
    if not record:
        return None
    for candidate in ("NORMALIZED_PE", "PE", "P_E"):
        if candidate in record and record[candidate] not in (None, ""):
            return record[candidate]
    for key in record:
        if key.upper().replace("/", "_") in {"NORMALIZED_PE", "PE"}:
            value = record[key]
            if value not in (None, ""):
                return value
    return None


def fetch_marketdata(
    secids: List[str],
    engine: str,
    market: str,
    board: str,
) -> List[Dict[str, object]]:
    joined = ",".join(secids)
    params = urllib.parse.urlencode(
        {
            "securities": joined,
            "securities.columns": "SECID,SHORTNAME,ISIN",
            "marketdata.columns": "SECID,BOARDID,NORMALIZED_PE,PE",
        }
    )
    url = (
        f"{BASE_URL}/engines/{engine}/markets/{market}/boards/{board}/securities.json"
        f"?{params}"
    )
    payload = fetch_json(url)
    sec_columns, sec_rows = extract_columns(payload.get("securities", {}))
    mkt_columns, mkt_rows = extract_columns(payload.get("marketdata", {}))
    securities_map = parse_block_map(sec_columns, sec_rows, "SECID")
    market_map = parse_block_map(mkt_columns, mkt_rows, "SECID")

    results = []
    for secid in secids:
        sec_record = securities_map.get(secid, {})
        mkt_record = market_map.get(secid, {})
        normalized_pe = normalized_pe_value(mkt_record) or normalized_pe_value(sec_record)
        results.append(
            {
                "secid": secid,
                "shortname": sec_record.get("SHORTNAME"),
                "isin": sec_record.get("ISIN"),
                "board": mkt_record.get("BOARDID", board),
                "normalized_pe": normalized_pe,
            }
        )
    return results


def collect_normalized_pe(
    index_code: str,
    engine: str,
    market: str,
    board: str,
    batch_size: int,
) -> List[Dict[str, object]]:
    secids = index_constituents(index_code)
    results: List[Dict[str, object]] = []
    for chunk in chunked(secids, batch_size):
        results.extend(fetch_marketdata(chunk, engine, market, board))
    return results


def write_csv(rows: List[Dict[str, object]], output: str) -> None:
    fieldnames = ["secid", "shortname", "isin", "board", "normalized_pe", "as_of"]
    with open(output, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        today = date.today().isoformat()
        for row in rows:
            row_with_date = dict(row)
            row_with_date["as_of"] = today
            writer.writerow(row_with_date)


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect normalized P/E values for MOEX index constituents.",
    )
    parser.add_argument("--index", default=DEFAULT_INDEX, help="MOEX index code (default: IMOEX)")
    parser.add_argument("--engine", default=DEFAULT_ENGINE, help="MOEX engine (default: stock)")
    parser.add_argument("--market", default=DEFAULT_MARKET, help="MOEX market (default: shares)")
    parser.add_argument("--board", default=DEFAULT_BOARD, help="Trading board (default: TQBR)")
    parser.add_argument("--batch", type=int, default=50, help="Batch size for ISS requests")
    parser.add_argument(
        "--output",
        default=f"moex_normalized_pe_{DEFAULT_INDEX.lower()}.csv",
        help="Output CSV path",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Write CSV headers even if data collection fails",
    )
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    output = args.output
    default_output = f"moex_normalized_pe_{DEFAULT_INDEX.lower()}.csv"
    if output == default_output and args.index.lower() != DEFAULT_INDEX.lower():
        output = f"moex_normalized_pe_{args.index.lower()}.csv"
    try:
        rows = collect_normalized_pe(args.index, args.engine, args.market, args.board, args.batch)
    except MoexError as exc:
        if not args.allow_empty:
            raise
        rows = []
        print(f"Warning: {exc}. Writing empty CSV to {output}.")
    write_csv(rows, output)
    print(f"Saved {len(rows)} rows to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
