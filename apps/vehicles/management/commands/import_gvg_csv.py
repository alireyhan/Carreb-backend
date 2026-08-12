"""Import Green Vehicle Guide CSVs into GreenVehicleGuideRecord.

Decision Note - Vehicle Internet Lookup (8-Aug-2026), Option 1. Header-DRIVEN:
columns are resolved via HEADER_ALIASES against the file's actual header row, and
`--dry-run` reports the mapping (incl. unmapped columns) without writing, so the
command survives GVG renaming columns between releases. Confirm the real headers
on first run (`--dry-run`), per the Decision Note's step 1.

Usage:
  python manage.py import_gvg_csv path/to/gvg_current.csv --dataset current \
      --source-url https://www.greenvehicleguide.gov.au/ [--dry-run] [--limit N]
  python manage.py import_gvg_csv path/to/gvg_1986_2003.csv --dataset older-8603 ...

Refresh cadence: re-download + re-import on the quarterly census cadence (idempotent
upsert on brand/model/variant/year). Collision rule: a CURRENT row is never
overwritten by an OLDER-8603 row for the same identity.
"""
import csv
import re

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.vehicles.models import GreenVehicleGuideRecord, GVGSourceDataset, PowertrainType

# Lower-cased, punctuation-stripped header -> model field. Extend as GVG evolves.
HEADER_ALIASES = {
    # identity
    'brand': 'brand', 'make': 'brand', 'manufacturer': 'brand',
    'model': 'model',
    'variant': 'variant', 'series': 'variant', 'description': 'variant', 'badge': 'variant',
    'year': 'year', 'model year': 'year', 'make year': 'year', 'year group': 'year',
    # descriptive
    'body style': 'body_style', 'body type': 'body_style', 'body': 'body_style',
    'engine': 'engine_desc', 'engine description': 'engine_desc', 'engine size': 'engine_desc',
    'engine capacity': 'engine_desc', 'engine size l': 'engine_desc',
    'fuel type': 'fuel_type_raw', 'fuel': 'fuel_type_raw', 'fuel lifecycle': 'fuel_type_raw',
    # efficiency (the two fields the COO/CO2 baseline needs)
    'combined fuel consumption l100km': 'fuel_consumption_per_100km',
    'fuel consumption combined l100km': 'fuel_consumption_per_100km',
    'combined l100km': 'fuel_consumption_per_100km',
    'fuel consumption': 'fuel_consumption_per_100km',
    'comb l100km': 'fuel_consumption_per_100km',
    'combined co2 gkm': 'tailpipe_co2_g_per_km',
    'co2 emissions combined gkm': 'tailpipe_co2_g_per_km',
    'co2 gkm': 'tailpipe_co2_g_per_km',
    'co2': 'tailpipe_co2_g_per_km',
    'tailpipe co2': 'tailpipe_co2_g_per_km',
    'energy consumption whkm': 'electric_energy_wh_per_km',
    'electric energy consumption': 'electric_energy_wh_per_km',
    'electric range km': 'electric_range_km', 'electric range': 'electric_range_km',
    'range km': 'electric_range_km',
    # provenance
    'test cycle': 'test_cycle', 'standard': 'test_cycle',
}

# GVG fuel labels -> Carreb's canonical 7 powertrains (best-effort; blank if unknown)
FUEL_TO_POWERTRAIN = [
    (re.compile(r'plug.?in|phev', re.I), PowertrainType.PHEV),
    (re.compile(r'hydrogen|fcev|fuel.?cell', re.I), PowertrainType.FCEV),
    (re.compile(r'electric|bev|^ev$', re.I), PowertrainType.BEV),
    (re.compile(r'mild.?hybrid|mhev', re.I), PowertrainType.MILD_HYBRID),
    (re.compile(r'hybrid|hev', re.I), PowertrainType.HYBRID),
    (re.compile(r'diesel', re.I), PowertrainType.DIESEL),
    (re.compile(r'petrol|gasoline|unleaded|ulp|premium', re.I), PowertrainType.PETROL),
]

DATASET_CHOICES = {
    'current': GVGSourceDataset.CURRENT,
    'older-8603': GVGSourceDataset.OLDER_8603,
}

def norm_header(h):
    h = re.sub(r'[/\-_().]', ' ', (h or '').strip().lower())
    h = re.sub(r'[^a-z0-9 ]', '', h)
    return re.sub(r'\s+', ' ', h).strip()


def parse_float(v):
    if v in (None, ''):
        return None
    m = re.search(r'-?\d+(?:\.\d+)?', str(v).replace(',', ''))
    return float(m.group()) if m else None


def parse_year(v):
    m = re.search(r'(19|20)\d{2}', str(v or ''))
    return int(m.group()) if m else None


def map_powertrain(fuel_raw):
    for pat, pt in FUEL_TO_POWERTRAIN:
        if pat.search(fuel_raw or ''):
            return pt
    return ''


def map_test_cycle(v):
    v = (v or '').strip().upper()
    for cycle in ('WLTP', 'ADR', 'NEDC'):
        if cycle in v:
            return cycle
    return ''  # never guessed - blank when the CSV doesn't state it


class Command(BaseCommand):
    help = 'Import a Green Vehicle Guide CSV into GreenVehicleGuideRecord (idempotent upsert).'

    def add_arguments(self, parser):
        parser.add_argument('csv_path')
        parser.add_argument('--dataset', required=True, choices=sorted(DATASET_CHOICES))
        parser.add_argument('--source-url', default='https://www.greenvehicleguide.gov.au/')
        parser.add_argument('--dry-run', action='store_true',
                            help='Parse + report the header mapping and row counts; write nothing.')
        parser.add_argument('--limit', type=int, default=None, help='Import at most N data rows.')

    def handle(self, csv_path, dataset, source_url, dry_run, limit, **options):
        source_dataset = DATASET_CHOICES[dataset]
        retrieved_at = timezone.now()

        try:
            fh = open(csv_path, newline='', encoding='utf-8-sig')
        except OSError as e:
            raise CommandError(f'Cannot open {csv_path}: {e}')

        with fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames:
                raise CommandError('CSV has no header row.')

            mapping, unmapped = {}, []
            for col in reader.fieldnames:
                field = HEADER_ALIASES.get(norm_header(col))
                if field and field not in mapping.values():
                    mapping[col] = field
                else:
                    unmapped.append(col)

            self.stdout.write(self.style.MIGRATE_HEADING(f'Header mapping ({csv_path}):'))
            for col, field in mapping.items():
                self.stdout.write(f'  {col!r:45} -> {field}')
            for col in unmapped:
                self.stdout.write(self.style.WARNING(f'  {col!r:45} -> (unmapped, kept in raw_row)'))

            required = {'brand', 'model', 'year'}
            missing = required - set(mapping.values())
            if missing:
                raise CommandError(
                    f'Required identity columns not found: {sorted(missing)}. '
                    f'Add the real header names to HEADER_ALIASES (see --dry-run output above).'
                )

            created = updated = skipped_older = skipped_bad = seen = 0
            with transaction.atomic():
                for row in reader:
                    if limit is not None and seen >= limit:
                        break
                    seen += 1

                    data = {field: (row.get(col) or '').strip() for col, field in mapping.items()}
                    year = parse_year(data.get('year'))
                    brand, model_name = data.get('brand', ''), data.get('model', '')
                    if not (brand and model_name and year):
                        skipped_bad += 1
                        continue

                    identity = {
                        'brand': brand, 'model': model_name,
                        'variant': data.get('variant', ''), 'year': year,
                    }
                    fuel_raw = data.get('fuel_type_raw', '')
                    defaults = {
                        'body_style': data.get('body_style', ''),
                        'engine_desc': data.get('engine_desc', ''),
                        'fuel_type_raw': fuel_raw,
                        'powertrain_type': map_powertrain(fuel_raw),
                        'fuel_consumption_per_100km': parse_float(data.get('fuel_consumption_per_100km')),
                        'electric_energy_wh_per_km': parse_float(data.get('electric_energy_wh_per_km')),
                        'tailpipe_co2_g_per_km': parse_float(data.get('tailpipe_co2_g_per_km')),
                        'electric_range_km': parse_float(data.get('electric_range_km')),
                        'test_cycle': map_test_cycle(data.get('test_cycle')),
                        'source_dataset': source_dataset,
                        'source_url': source_url,
                        'retrieved_at': retrieved_at,
                        'raw_row': dict(row),
                    }

                    existing = GreenVehicleGuideRecord.objects.filter(**identity).first()
                    if existing:
                        # Collision rule: never let the older (different-test-cycle)
                        # dataset overwrite a CURRENT row for the same identity.
                        if (existing.source_dataset == GVGSourceDataset.CURRENT
                                and source_dataset == GVGSourceDataset.OLDER_8603):
                            skipped_older += 1
                            continue
                        for k, v in defaults.items():
                            setattr(existing, k, v)
                        existing.save()
                        updated += 1
                    else:
                        GreenVehicleGuideRecord.objects.create(**identity, **defaults)
                        created += 1

                if dry_run:
                    transaction.set_rollback(True)

        verb = 'DRY-RUN (rolled back)' if dry_run else 'Imported'
        self.stdout.write(self.style.SUCCESS(
            f'{verb}: {created} created, {updated} updated, '
            f'{skipped_older} kept-current (older skipped), {skipped_bad} rows missing identity, '
            f'dataset={source_dataset}, retrieved_at={retrieved_at.isoformat()}'
        ))
        self.stdout.write(
            'SourceRegistry: record this file as '
            f'{source_dataset} with url={source_url} retrieved={retrieved_at.date()} '
            'and archive a copy of the CSV alongside the substantiation trail.'
        )
