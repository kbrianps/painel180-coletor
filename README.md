# Ligue 180 panel collector

Periodically collects the women's assistance services published on Brazil's
[Painel da Rede de Atendimento](https://www.gov.br/mulheres/pt-br/ligue180/painel-da-rede-de-atendimento)
(Ministry of Women) and keeps a history of every version that actually changed.

Python 3 only — no third-party dependencies.

## How the data is obtained

The panel is a public **Power BI** report embedded in the gov.br page. There is no HTML scraping:
the script talks to the public Power BI API in two steps.

1. `GET .../modelsAndExploration` returns the report's data model (dataset, model id and the date
   of the last refresh).
2. `POST .../querydata` runs a semantic query against that model and returns the rows.

The query is **built by the script** over the `dEndereço` and `dServico` entities rather than
copied from one of the panel's visuals. Visuals disappear or change identifiers whenever the
dashboard is redesigned; model fields are stable. The visual used by an earlier manual extraction
no longer exists.

Responses arrive compressed: repeated values become a bitmask (`R`), nulls another one (`Ø`), and
text values become indices into per-column dictionaries (`ValueDicts`). `parse_rows` undoes that.

## What it writes

Every collection that **changes** the content writes:

| Path | Content |
|---|---|
| `panel_services.csv` | one service per row, 19 fields, plus source URL and dates — nationwide |
| `panel_services.json` | the same rows unformatted, with the model's metadata |
| `states/<CODE>/…` | the same two files restricted to one state (only with `--state`) |

The nationwide file is always written. The state slice is additional, for whoever follows one
region without losing the national picture.

When nothing changes, nothing is written: the script compares a `sha256` of the rows against
`last_fingerprint.txt` and just logs `unchanged`. This matters because the panel is refreshed
sporadically — between 2026-08-12 and 2026-09-19 the data did not change once.

Without `--repo`, each change becomes a timestamped folder and the oldest ones are deleted,
keeping the `--keep` most recent.

### Repository mode

With `--repo` pointing at a Git clone, the script always writes to the same paths under `data/`
and commits and pushes on every change. Git history becomes the panel's history: each commit is a
real change, and the diff shows which services were added, removed or corrected. In this mode
there is no rotation and the number of versions is unlimited.

On an unattended machine, use a deploy key scoped to that single repository rather than a
credential covering your whole account.

## Configuration

Every option has an environment variable counterpart, which is convenient with systemd:

| Option | Variable | Default | Purpose |
|---|---|---|---|
| `--state` | `PANEL180_STATE` | empty | extra slice for one state (e.g. `RJ`) |
| `--dir` | `PANEL180_DIR` | `~/panel180/data` | where to write |
| `--repo` | `PANEL180_REPO` | empty | Git clone to write into and commit |
| `--keep` | `PANEL180_KEEP` | `5` | versions kept when `--repo` is not used |
| `--minimum` | `PANEL180_MINIMUM` | `100` | minimum acceptable row count |
| `--resource-key` | `PANEL180_RESOURCE_KEY` | the panel's | Power BI report identifier |
| `--api` | `PANEL180_API` | Brazil South | API endpoint, which varies by region |
| `--timeout` | `PANEL180_TIMEOUT` | `60` | per-request timeout, in seconds |

Three flags have no variable: `--dry-run` collects and prints a summary without writing, `--force`
writes even without changes, and `--quiet` prints errors only.

```bash
python3 collect_panel180.py --dry-run           # see what would come back
python3 collect_panel180.py --state BA          # nationwide, plus a Bahia slice
python3 collect_panel180.py --repo ~/my-clone   # automatic commit on every change
```

Because `--resource-key` and `--api` are configurable, the script also works against other public
Power BI reports of similar shape: adjust `FIELDS` with the entities and properties of that model.

CSV column names stay in Portuguese because they mirror the source dataset, whose values are
Brazilian addresses and service names.

## Safeguards

The script refuses to write, and exits non-zero, when:

- fewer than `--minimum` services come back nationwide (suggests an API change or a failure);
- the state column disappears from the response;
- the chosen state ends up with no services;
- the network request fails or times out.

In all of those cases the data already collected stays untouched.

## Installation

```bash
mkdir -p ~/panel180 && cp collect_panel180.py ~/panel180/
sudo cp systemd/panel180.service systemd/panel180.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now panel180.timer
```

Adjust the `Environment=` lines in `panel180.service` to choose the state, the destination or the
repository. To run once, by hand:

```bash
python3 ~/panel180/collect_panel180.py
```

To follow along:

```bash
systemctl list-timers panel180.timer
journalctl -u panel180.service -n 20
```

## Origin

The equivalent data was first extracted by hand on 2026-08-24, for Rio de Janeiro only (180
services). This script reproduces that result exactly — the 180 rows match field by field — and
widens the scope to the 2,641 services across the country.

## License

[CC0 1.0](LICENSE) — public domain dedication. The underlying data is already public, published
by a federal government agency; this waives any claim over the code and the collected snapshots
too, so anyone can reuse either without asking.
