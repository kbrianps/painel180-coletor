#!/usr/bin/env python3
"""Collect Brazil's "Painel da Rede de Atendimento" (Ligue 180) from the Power BI API.

The dashboard published by the Brazilian Ministry of Women is a public Power BI
report. This script queries the public Power BI API, decompresses the response and
writes CSV and JSON. No HTML scraping, no third-party dependencies.

Quick start:
    python3 collect_panel180.py                  # whole country
    python3 collect_panel180.py --state RJ       # country plus one state slice
    python3 collect_panel180.py --repo ~/clone   # write and commit to a Git repo

Every option has an equivalent environment variable (PANEL180_*), handy with systemd.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

PANEL_URL = "https://www.gov.br/mulheres/pt-br/ligue180/painel-da-rede-de-atendimento"
REPORT_NAME = "Painel da Rede de Atendimento à Mulher"
RESOURCE_KEY_DEFAULT = "a1b972db-9924-4fcd-b04a-fd619fb27007"
API_DEFAULT = "https://wabi-brazil-south-d-primary-api.analysis.windows.net/public/reports"

# Fields requested from the data model: (alias, entity, properties).
# Property names are Portuguese because that is how the source model names them.
FIELDS = [
    ("d", "dEndereço", ["UF", "Municipio", "Municipio + UF", "Estado", "Unidade Federativa",
                        "Regiao", "CEP", "Endereco", "Bairro", "Combinação Endereço",
                        "Latitude", "Longitude"]),
    ("s", "dServico", ["Tipo Serviço", "Nome do Serviço", "Telefone", "OutroTelefone",
                       "Email", "Site", "RedeSocial"]),
]

# CSV column names. They mirror the source dataset, so they stay in Portuguese:
# the values are Brazilian addresses and service names anyway.
CSV_HEADERS = {
    "dEndereço.UF": "UF",
    "dEndereço.Municipio": "Municipio",
    "dEndereço.Municipio + UF": "Municipio_UF",
    "dEndereço.Estado": "Estado",
    "dEndereço.Unidade Federativa": "Unidade_Federativa",
    "dEndereço.Regiao": "Regiao",
    "dEndereço.CEP": "CEP",
    "dEndereço.Endereco": "Endereco",
    "dEndereço.Bairro": "Bairro",
    "dEndereço.Combinação Endereço": "Endereco_Completo",
    "dEndereço.Latitude": "Latitude",
    "dEndereço.Longitude": "Longitude",
    "dServico.Tipo Serviço": "Tipo_Servico",
    "dServico.Nome do Serviço": "Nome_Servico",
    "dServico.Telefone": "Telefone",
    "dServico.OutroTelefone": "Outro_Telefone",
    "dServico.Email": "Email",
    "dServico.Site": "Site",
    "dServico.RedeSocial": "Rede_Social",
}

STATE_COLUMN = "dEndereço.UF"
VALID_STATES = {
    "AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS", "MT",
    "PA", "PB", "PE", "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC", "SE", "SP", "TO",
}


# ----------------------------------------------------------------------- options


def env(name: str, default: str = "") -> str:
    return os.environ.get(f"PANEL180_{name}", default).strip()


def parse_options(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect the Painel da Rede de Atendimento (Ligue 180).",
        epilog="Each option has an environment variable counterpart: PANEL180_STATE, "
               "PANEL180_DIR, PANEL180_REPO, PANEL180_KEEP, PANEL180_MINIMUM, "
               "PANEL180_RESOURCE_KEY, PANEL180_API, PANEL180_TIMEOUT.",
    )
    parser.add_argument("--state", default=env("STATE"),
                        help="two-letter state code (e.g. RJ). Writes an extra slice for that "
                             "state alongside the nationwide files. Default: nationwide only.")
    parser.add_argument("--dir", dest="directory", default=env("DIR"),
                        help="where to write (default: ~/panel180/data).")
    parser.add_argument("--repo", default=env("REPO"),
                        help="path to a Git clone: writes into <repo>/data and commits on every "
                             "change, using Git history instead of rotating folders.")
    parser.add_argument("--keep", type=int, default=int(env("KEEP", "5") or 5),
                        help="how many versions to keep when --repo is not used (default: 5).")
    parser.add_argument("--minimum", type=int, default=int(env("MINIMUM", "100") or 100),
                        help="refuse to write if fewer rows come back (default: 100).")
    parser.add_argument("--resource-key", default=env("RESOURCE_KEY", RESOURCE_KEY_DEFAULT),
                        help="Power BI report identifier.")
    parser.add_argument("--api", default=env("API", API_DEFAULT),
                        help="Power BI API endpoint, which varies by report region.")
    parser.add_argument("--timeout", type=int, default=int(env("TIMEOUT", "60") or 60),
                        help="per-request timeout in seconds (default: 60).")
    parser.add_argument("--force", action="store_true",
                        help="write even when the content has not changed.")
    parser.add_argument("--dry-run", action="store_true",
                        help="collect and print a summary without writing anything.")
    parser.add_argument("--quiet", action="store_true", help="print errors only.")
    options = parser.parse_args(argv)

    options.state = options.state.strip().upper()
    if options.state and options.state not in VALID_STATES:
        parser.error(f"invalid state: {options.state}. Use one of the 27 codes, or leave empty.")
    options.directory = (Path(options.directory) if options.directory
                         else Path.home() / "panel180" / "data")
    return options


# --------------------------------------------------------------------------- api


def request_json(url: str, body: bytes | None, resource_key: str, timeout: int) -> dict:
    headers = {
        "ActivityId": str(uuid.uuid4()),
        "RequestId": str(uuid.uuid4()),
        "X-PowerBI-ResourceKey": resource_key,
        "Origin": "https://app.powerbi.com",
        "Referer": "https://app.powerbi.com/",
        "Accept": "application/json",
        "User-Agent": "panel180-collector (https://github.com/kbrianps/painel180-coletor)",
    }
    if body is not None:
        headers["Content-Type"] = "application/json;charset=UTF-8"
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method="POST" if body else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        payload = response.read()
        if (response.headers.get("Content-Encoding", "").lower() == "gzip"
                or payload[:2] == b"\x1f\x8b"):
            payload = gzip.decompress(payload)
        return json.loads(payload.decode("utf-8"))


def build_query() -> dict:
    """Build the query against the data model instead of copying one from a visual.

    Visuals disappear or change identifiers whenever the dashboard is redesigned;
    model fields are stable. The visual used by the manual extraction of 2026-08 is
    already gone."""
    projections = [
        {"Column": {"Expression": {"SourceRef": {"Source": alias}}, "Property": prop},
         "Name": f"{entity}.{prop}"}
        for alias, entity, properties in FIELDS
        for prop in properties
    ]
    return {"Commands": [{"SemanticQueryDataShapeCommand": {
        "Query": {
            "Version": 2,
            "From": [{"Name": alias, "Entity": entity, "Type": 0}
                     for alias, entity, _ in FIELDS],
            "Select": projections,
        },
        "Binding": {
            "Primary": {"Groupings": [{"Projections": list(range(len(projections)))}]},
            "DataReduction": {"DataVolume": 6, "Primary": {"Window": {"Count": 30000}}},
            "Version": 1,
        },
    }}]}


def read_model_info(model: dict) -> dict:
    first = (model.get("models") or [{}])[0]
    refreshed = str(first.get("lastRefreshTime") or "")
    found = re.search(r"/Date\((\d+)\)/", refreshed)
    if found:
        refreshed = datetime.fromtimestamp(int(found.group(1)) / 1000, timezone.utc).isoformat()
    return {"datasetId": first.get("dbName") or first.get("datasetId"),
            "modelId": first.get("id"), "lastRefresh": refreshed}


def collect(options: argparse.Namespace) -> tuple[list[str], list[list], dict]:
    model = request_json(
        f"{options.api}/{options.resource_key}/modelsAndExploration?preferReadOnlySession=true",
        None, options.resource_key, options.timeout)
    info = read_model_info(model)
    body = {
        "version": "1.0.0",
        "queries": [{"Query": build_query(),
                     "ApplicationContext": {"DatasetId": info["datasetId"],
                                            "Sources": [{"ReportId": options.resource_key}]}}],
        "cancelQueries": [],
        "modelId": info["modelId"],
    }
    response = request_json(f"{options.api}/querydata?synchronous=true",
                            json.dumps(body, ensure_ascii=False).encode("utf-8"),
                            options.resource_key, options.timeout)
    columns, rows = parse_rows(response)
    return columns, rows, info


def parse_rows(response: dict) -> tuple[list[str], list[list]]:
    """Undo the Power BI DSR compression.

    Each row carries only the values that changed: `R` is a bitmask telling which
    columns repeat the previous row's value, `Ø` marks nulls, and text values arrive
    as indices into per-column dictionaries in `ValueDicts`."""
    data = response["results"][0]["result"]["data"]
    columns = [item["Name"] for item in data["descriptor"]["Select"]]
    dataset = data["dsr"]["DS"][0]
    dictionaries = dataset.get("ValueDicts", {})
    raw_rows = dataset["PH"][0]["DM0"]

    dict_of_column = {index: field["DN"]
                      for index, field in enumerate(raw_rows[0].get("S", []))
                      if field.get("DN")}

    rows: list[list] = []
    previous: list = [None] * len(columns)
    for raw in raw_rows:
        given = raw.get("C", [])
        repeat_mask, null_mask = raw.get("R", 0), raw.get("Ø", 0)
        row: list = []
        position = 0
        for index in range(len(columns)):
            if null_mask >> index & 1:
                value = None
            elif repeat_mask >> index & 1:
                value = previous[index]
            else:
                value = given[position] if position < len(given) else None
                position += 1
                if index in dict_of_column and isinstance(value, int):
                    table = dictionaries.get(dict_of_column[index], [])
                    if 0 <= value < len(table):
                        value = table[value]
            row.append(value)
        previous = row
        rows.append(row)
    return columns, rows


# ------------------------------------------------------------------------- output


def write_files(target: Path, columns: list[str], rows: list[list], info: dict,
                scope: str, country_total: int) -> None:
    target.mkdir(parents=True, exist_ok=True)
    document = {
        "sourceUrl": PANEL_URL,
        "reportName": REPORT_NAME,
        "datasetId": info.get("datasetId"),
        "modelId": info.get("modelId"),
        "lastRefresh": info.get("lastRefresh"),
        "extractedAt": datetime.now(timezone.utc).isoformat(),
        "scope": scope,
        "rowCountCountry": country_total,
        "columns": columns,
        "rowCount": len(rows),
        "rows": rows,
    }
    (target / "panel_services.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")

    names = [CSV_HEADERS.get(c, c.split(".")[-1].replace(" ", "_")) for c in columns]
    with (target / "panel_services.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(names + ["Fonte_URL", "Data_Extracao", "Ultima_Atualizacao_Modelo"])
        for row in rows:
            writer.writerow(["" if value is None else value for value in row]
                            + [PANEL_URL, document["extractedAt"], document["lastRefresh"]])


def filter_state(columns: list[str], rows: list[list], state: str) -> list[list]:
    if STATE_COLUMN not in columns:
        return []
    index = columns.index(STATE_COLUMN)
    return [row for row in rows if str(row[index] or "").strip().upper() == state]


def publish_to_git(repo: Path, message: str) -> str:
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(repo), *args],
                              capture_output=True, text=True, timeout=120)

    git("add", "data")
    if not git("status", "--porcelain", "data").stdout.strip():
        return "nothing to commit"
    committed = git("commit", "-m", message)
    if committed.returncode != 0:
        return f"commit failed: {committed.stderr.strip()[:200]}"
    pushed = git("push")
    if pushed.returncode != 0:
        return f"committed, push failed: {pushed.stderr.strip()[:200]}"
    return "committed and pushed"


def prune(base: Path, keep: int) -> list[str]:
    versions = sorted((path for path in base.iterdir() if path.is_dir()), reverse=True)
    removed = []
    for old in versions[keep:]:
        shutil.rmtree(old, ignore_errors=True)
        removed.append(old.name)
    return removed


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    options = parse_options(argv)
    say = (lambda *args: None) if options.quiet else print

    try:
        columns, rows, info = collect(options)
    except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError) as error:
        print(f"ERROR while collecting: {type(error).__name__}: {error}", file=sys.stderr)
        return 2

    country_total = len(rows)
    if country_total < options.minimum:
        print(f"ERROR: only {country_total} rows nationwide (minimum {options.minimum})."
              " The API may have changed. Nothing was written.", file=sys.stderr)
        return 1

    state_rows: list[list] = []
    if options.state:
        state_rows = filter_state(columns, rows, options.state)
        if not state_rows:
            print(f"ERROR: no services in {options.state} among {country_total}."
                  " Nothing was written.", file=sys.stderr)
            return 1

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    refreshed = (info.get("lastRefresh") or "?")[:10]
    summary = f"{country_total} services nationwide"
    if options.state:
        summary += f", {len(state_rows)} in {options.state}"

    if options.dry_run:
        say(f"[{timestamp}] dry run: {summary} | model refreshed on {refreshed}")
        return 0

    fingerprint = hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    base = Path(options.repo) / "data" if options.repo else options.directory
    base.mkdir(parents=True, exist_ok=True)
    stamp = base / "last_fingerprint.txt"

    if not options.force and stamp.exists() and stamp.read_text().strip() == fingerprint:
        say(f"[{timestamp}] unchanged ({summary})")
        return 0

    target = base if options.repo else options.directory / datetime.now(
        timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    write_files(target, columns, rows, info, "BR", country_total)
    if options.state:
        write_files(target / "states" / options.state, columns, state_rows, info,
                    options.state, country_total)
    stamp.write_text(fingerprint)

    if options.repo:
        label = f"Brazil: {country_total} services"
        if options.state:
            label += f" | {options.state}: {len(state_rows)}"
        status = publish_to_git(Path(options.repo), f"{label} (model refreshed {refreshed})")
        say(f"[{timestamp}] CHANGED: {summary} | {status}")
        return 0

    removed = prune(options.directory, options.keep)
    say(f"[{timestamp}] CHANGED: {summary} -> {target.name}"
        + (f" | removed: {', '.join(removed)}" if removed else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
