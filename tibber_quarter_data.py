#!/usr/bin/env python3
"""
Fetch QUARTER_HOURLY consumption data from the Tibber GraphQL API.

Usage:
  python tibber_quarter_data.py --last 96          # last 24 hours (default)
  python tibber_quarter_data.py --from 2024-01-01 --to 2024-01-07
  python tibber_quarter_data.py --from 2024-01-01 --to 2024-01-07 --format csv --out data.csv
  python tibber_quarter_data.py --homes             # list available homes
"""

import os
import sys
import json
import csv
import io
import argparse
from datetime import datetime, date, timedelta, timezone
from typing import Optional
import requests
from dotenv import load_dotenv

load_dotenv()

TIBBER_API_URL = "https://api.tibber.com/v1-beta/gql"
MAX_PER_REQUEST = 744  # safe upper limit per API call

HOMES_QUERY = """
{
  viewer {
    homes {
      id
      appNickname
      address {
        address1
        postalCode
        city
        country
      }
    }
  }
}
"""

CONSUMPTION_QUERY = """
query QuarterData($homeId: ID!, $last: Int!, $before: String) {
  viewer {
    home(id: $homeId) {
      consumption(resolution: QUARTER_HOURLY, last: $last, before: $before) {
        pageInfo {
          startCursor
          endCursor
          hasPreviousPage
        }
        nodes {
          from
          to
          cost
          unitPrice
          unitPriceVAT
          consumption
          consumptionUnit
          currency
        }
      }
    }
  }
}
"""


class TibberClient:
    def __init__(self, token: str):
        self.token = token
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })

    def _gql(self, query: str, variables: dict = None) -> dict:
        payload = {"query": query}
        if variables:
            payload["variables"] = variables

        resp = self.session.post(TIBBER_API_URL, json=payload, timeout=30)

        if resp.status_code == 401:
            raise SystemExit("Error: ongeldige API-token. Controleer TIBBER_TOKEN.")
        resp.raise_for_status()

        body = resp.json()
        if "errors" in body:
            raise RuntimeError(f"GraphQL fout: {body['errors']}")
        return body["data"]

    def get_homes(self) -> list:
        data = self._gql(HOMES_QUERY)
        return data["viewer"]["homes"]

    def get_quarter_data(
        self,
        home_id: str,
        start: Optional[date] = None,
        end: Optional[date] = None,
        last: int = 96,
    ) -> list:
        """
        Fetch quarter-hourly records.

        - Without dates: returns the most recent `last` records.
        - With start/end:  fetches all records in [start, end] (inclusive).
          Records with a null consumption value are included as-is.
        """
        if start and end:
            days = (end - start).days + 1
            last = days * 96  # 4 kwartieren * 24 uur

        all_nodes: list = []
        cursor: Optional[str] = None

        while True:
            batch = min(last - len(all_nodes), MAX_PER_REQUEST)
            variables: dict = {"homeId": home_id, "last": batch}
            if cursor:
                variables["before"] = cursor

            data = self._gql(CONSUMPTION_QUERY, variables)
            page = data["viewer"]["home"]["consumption"]
            nodes = page["nodes"]
            page_info = page["pageInfo"]

            all_nodes = nodes + all_nodes  # prepend older records

            if not page_info["hasPreviousPage"]:
                break
            if len(all_nodes) >= last:
                break

            cursor = page_info["startCursor"]

        # Filter on date range when requested
        if start or end:
            def in_range(node: dict) -> bool:
                node_dt = datetime.fromisoformat(
                    node["from"].replace("Z", "+00:00")
                ).date()
                if start and node_dt < start:
                    return False
                if end and node_dt > end:
                    return False
                return True

            all_nodes = [n for n in all_nodes if in_range(n)]

        return all_nodes


# ── Output formatters ──────────────────────────────────────────────────────────

def _to_csv(nodes: list) -> str:
    if not nodes:
        return ""
    fields = ["from", "to", "consumption", "consumptionUnit",
              "cost", "currency", "unitPrice", "unitPriceVAT"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(nodes)
    return buf.getvalue()


def _to_table(nodes: list) -> str:
    if not nodes:
        return "(geen data)"
    lines = [
        f"{'Van':<22} {'Tot':<22} {'kWh':>8} {'Kosten':>10} {'Valuta':<5} {'Prijs/kWh':>10}"
    ]
    lines.append("-" * 82)
    total_kwh = 0.0
    total_cost = 0.0
    currency = ""
    for n in nodes:
        kwh = n["consumption"] or 0.0
        cost = n["cost"] or 0.0
        total_kwh += kwh
        total_cost += cost
        currency = n.get("currency") or ""
        lines.append(
            f"{n['from']:<22} {n['to']:<22} {kwh:>8.3f} {cost:>10.4f} "
            f"{currency:<5} {(n['unitPrice'] or 0):>10.5f}"
        )
    lines.append("-" * 82)
    lines.append(
        f"{'Totaal':<45} {total_kwh:>8.3f} {total_cost:>10.4f} {currency:<5}"
    )
    return "\n".join(lines)


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Haal kwartierdata op via de Tibber API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--token",
        default=os.getenv("TIBBER_TOKEN"),
        help="Tibber API-token (of stel TIBBER_TOKEN in)",
    )
    parser.add_argument(
        "--home-id",
        default=os.getenv("TIBBER_HOME_ID"),
        help="Tibber home-ID (auto-detect als niet opgegeven)",
    )
    parser.add_argument(
        "--homes",
        action="store_true",
        help="Toon beschikbare homes en stop",
    )
    parser.add_argument(
        "--from",
        dest="from_date",
        metavar="YYYY-MM-DD",
        help="Startdatum (inclusief)",
    )
    parser.add_argument(
        "--to",
        dest="to_date",
        metavar="YYYY-MM-DD",
        help="Einddatum (inclusief, standaard: vandaag)",
    )
    parser.add_argument(
        "--last",
        type=int,
        default=96,
        help="Laatste N kwartierrecords ophalen (standaard: 96 = 24 uur)",
    )
    parser.add_argument(
        "--format",
        choices=["table", "csv", "json"],
        default="table",
        help="Uitvoerformaat (standaard: table)",
    )
    parser.add_argument(
        "--out",
        metavar="BESTAND",
        help="Sla uitvoer op in bestand",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if not args.token:
        sys.exit(
            "Fout: geen API-token. Stel TIBBER_TOKEN in of gebruik --token."
        )

    client = TibberClient(args.token)

    # --homes: toon beschikbare homes
    if args.homes:
        homes = client.get_homes()
        for h in homes:
            addr = h.get("address") or {}
            name = h.get("appNickname") or "(geen naam)"
            print(f"ID  : {h['id']}")
            print(f"Naam: {name}")
            print(f"Adres: {addr.get('address1','')} {addr.get('postalCode','')} {addr.get('city','')}")
            print()
        return

    # Home-ID bepalen
    home_id = args.home_id
    if not home_id:
        homes = client.get_homes()
        if not homes:
            sys.exit("Fout: geen homes gevonden in je Tibber-account.")
        home_id = homes[0]["id"]
        if len(homes) > 1:
            print(
                f"Meerdere homes gevonden, gebruik eerste: {home_id}\n"
                "Gebruik --home-id om een specifieke home te kiezen.\n",
                file=sys.stderr,
            )

    # Datumbereik verwerken
    start: Optional[date] = None
    end: Optional[date] = None

    if args.from_date:
        try:
            start = date.fromisoformat(args.from_date)
        except ValueError:
            sys.exit(f"Ongeldige --from datum: {args.from_date!r} (gebruik YYYY-MM-DD)")

    if args.to_date:
        try:
            end = date.fromisoformat(args.to_date)
        except ValueError:
            sys.exit(f"Ongeldige --to datum: {args.to_date!r} (gebruik YYYY-MM-DD)")
    elif start:
        end = date.today()

    # Data ophalen
    print(
        f"Kwartierdata ophalen voor home {home_id}"
        + (f" van {start} t/m {end}" if start else f" (laatste {args.last} records)")
        + " ...",
        file=sys.stderr,
    )

    nodes = client.get_quarter_data(
        home_id=home_id,
        start=start,
        end=end,
        last=args.last,
    )

    print(f"{len(nodes)} records opgehaald.", file=sys.stderr)

    # Opmaken
    if args.format == "json":
        output = json.dumps(nodes, indent=2, ensure_ascii=False)
    elif args.format == "csv":
        output = _to_csv(nodes)
    else:
        output = _to_table(nodes)

    # Wegschrijven
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="") as f:
            f.write(output)
        print(f"Opgeslagen in {args.out}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
