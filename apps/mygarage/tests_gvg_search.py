"""GVG current-vehicle lookup tests (Decision Note - Vehicle Internet Lookup, 8-Aug-2026).

Covers: the header-driven import_gvg_csv command (mapping, upsert, collision rule,
dry-run), the two-tier CurrentVehicleSearchView merge (catalogue-first dedup,
prior-year GVG hits, multi-word queries), and GVG-sourced current-vehicle creation
(server-side manual_entry_data enrichment with provenance).
Runs in CI against postgres:16 (exercises the pg_trgm path).
"""
import csv
import tempfile

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.mygarage.models import CurrentVehicle
from apps.vehicles.models import (
    GreenVehicleGuideRecord, GVGSourceDataset, PowertrainType, Vehicle,
)

GVG_HEADERS = [
    'Make', 'Model', 'Variant', 'Year', 'Body Type', 'Fuel Type',
    'Combined Fuel Consumption (L/100km)', 'Combined CO2 (g/km)', 'Engine',
]


def write_csv(rows, headers=GVG_HEADERS):
    f = tempfile.NamedTemporaryFile(
        'w', newline='', suffix='.csv', delete=False, encoding='utf-8')
    writer = csv.writer(f)
    writer.writerow(headers)
    writer.writerows(rows)
    f.close()
    return f.name


def make_gvg(**kw):
    defaults = dict(
        brand='Toyota', model='Corolla', variant='Ascent Sport', year=2019,
        body_style='Sedan', engine_desc='2.0L Petrol', fuel_type_raw='Petrol',
        powertrain_type=PowertrainType.PETROL,
        fuel_consumption_per_100km=6.0, tailpipe_co2_g_per_km=139.0,
        source_dataset=GVGSourceDataset.CURRENT,
    )
    defaults.update(kw)
    return GreenVehicleGuideRecord.objects.create(**defaults)


def make_vehicle(**kw):
    defaults = dict(
        brand='Toyota', model='RAV4', year=2026, variant='GX AWD',
        body_style='SUV', powertrain_type=PowertrainType.HYBRID,
        fcai_category='Medium SUV', test_cycle='WLTP',
        fuel_consumption_per_100km=4.8, tailpipe_co2_g_per_km=107.0,
    )
    defaults.update(kw)
    return Vehicle.objects.create(**defaults)


class ImportGvgCsvTests(TestCase):
    def test_import_creates_records_with_parsed_fields(self):
        path = write_csv([
            ['Toyota', 'Corolla', 'Ascent Sport', '2019', 'Sedan', 'Petrol', '6.0', '139', '2.0L'],
            ['Ford', 'Ranger', 'XLT', '2016', 'Ute', 'Diesel', '8.9', '234', '3.2L'],
        ])
        call_command('import_gvg_csv', path, dataset='current')
        self.assertEqual(GreenVehicleGuideRecord.objects.count(), 2)
        corolla = GreenVehicleGuideRecord.objects.get(model='Corolla')
        self.assertEqual(corolla.year, 2019)
        self.assertEqual(corolla.fuel_consumption_per_100km, 6.0)
        self.assertEqual(corolla.tailpipe_co2_g_per_km, 139.0)
        self.assertEqual(corolla.powertrain_type, PowertrainType.PETROL)
        self.assertEqual(corolla.source_dataset, GVGSourceDataset.CURRENT)
        self.assertIn('2019', corolla.search_text)
        self.assertIn('Corolla', corolla.search_text)
        self.assertTrue(corolla.raw_row)  # verbatim row kept for the audit trail

    def test_reimport_is_idempotent_upsert(self):
        path = write_csv([['Toyota', 'Corolla', 'Ascent Sport', '2019', 'Sedan', 'Petrol', '6.0', '139', '2.0L']])
        call_command('import_gvg_csv', path, dataset='current')
        path2 = write_csv([['Toyota', 'Corolla', 'Ascent Sport', '2019', 'Sedan', 'Petrol', '6.1', '140', '2.0L']])
        call_command('import_gvg_csv', path2, dataset='current')
        self.assertEqual(GreenVehicleGuideRecord.objects.count(), 1)
        self.assertEqual(GreenVehicleGuideRecord.objects.get().fuel_consumption_per_100km, 6.1)

    def test_older_dataset_never_overwrites_current(self):
        path = write_csv([['Toyota', 'Corolla', 'Ascent Sport', '2019', 'Sedan', 'Petrol', '6.0', '139', '2.0L']])
        call_command('import_gvg_csv', path, dataset='current')
        path_old = write_csv([['Toyota', 'Corolla', 'Ascent Sport', '2019', 'Sedan', 'Petrol', '7.5', '175', '2.0L']])
        call_command('import_gvg_csv', path_old, dataset='older-8603')
        rec = GreenVehicleGuideRecord.objects.get()
        self.assertEqual(rec.fuel_consumption_per_100km, 6.0)  # current kept
        self.assertEqual(rec.source_dataset, GVGSourceDataset.CURRENT)

    def test_dry_run_writes_nothing(self):
        path = write_csv([['Toyota', 'Corolla', 'Ascent Sport', '2019', 'Sedan', 'Petrol', '6.0', '139', '2.0L']])
        call_command('import_gvg_csv', path, dataset='current', dry_run=True)
        self.assertEqual(GreenVehicleGuideRecord.objects.count(), 0)

    def test_missing_identity_headers_raise(self):
        path = write_csv([['Sedan', 'Petrol']], headers=['Body Type', 'Fuel Type'])
        with self.assertRaises(CommandError):
            call_command('import_gvg_csv', path, dataset='current')

    def test_rows_missing_identity_are_skipped_not_fatal(self):
        path = write_csv([
            ['', 'Corolla', 'Ascent', '2019', 'Sedan', 'Petrol', '6.0', '139', '2.0L'],
            ['Ford', 'Ranger', 'XLT', '2016', 'Ute', 'Diesel', '8.9', '234', '3.2L'],
        ])
        call_command('import_gvg_csv', path, dataset='current')
        self.assertEqual(GreenVehicleGuideRecord.objects.count(), 1)


class CurrentVehicleSearchTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = reverse('current-vehicle-search')

    def search(self, query):
        return self.client.post(self.url, {'query': query}, format='json').json()

    def test_prior_year_vehicle_found_via_gvg(self):
        make_gvg()  # 2019 Corolla - NOT in the catalogue
        rows = self.search('corolla 2019')
        self.assertTrue(rows)
        self.assertEqual(rows[0]['source'], 'gvg')
        self.assertEqual(rows[0]['year'], 2019)
        self.assertIn('gvg_record_id', rows[0])
        self.assertEqual(rows[0]['fuel_consumption_per_100km'], 6.0)

    def test_multi_word_query_matches(self):
        # regression: the old single-string icontains never matched multi-word queries
        make_gvg()
        self.assertTrue(self.search('toyota corolla'))

    def test_catalogue_row_wins_dedup(self):
        v = make_vehicle(brand='Toyota', model='Corolla', variant='Ascent Sport',
                         year=2019, body_style='Sedan',
                         powertrain_type=PowertrainType.PETROL, fcai_category='Small')
        make_gvg()  # same identity in GVG
        rows = self.search('corolla 2019')
        sources = [r['source'] for r in rows]
        self.assertIn('catalogue', sources)
        keys = [(r['brand'], r['model'], r.get('variant'), r['year']) for r in rows]
        self.assertEqual(len(keys), len(set(keys)), 'same identity shown twice')
        self.assertEqual(rows[0]['source'], 'catalogue')
        self.assertEqual(str(rows[0]['id']), str(v.id))

    def test_catalogue_still_searched_first(self):
        make_vehicle()
        rows = self.search('rav4 2026')
        self.assertTrue(rows)
        self.assertEqual(rows[0]['source'], 'catalogue')

    def test_short_or_empty_query_returns_empty(self):
        make_gvg()
        self.assertEqual(self.search('co'), [])
        self.assertEqual(self.search(''), [])

    def test_no_match_returns_empty_list_never_errors(self):
        self.assertEqual(self.search('zzzz qqqq'), [])


class CurrentVehicleGvgCreateTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            email='gvg-test@carreb.com.au', password='x7&kPqTest!')
        self.client.force_authenticate(self.user)
        self.rec = make_gvg()
        self.url = reverse('current-vehicle')

    def test_create_from_gvg_record_enriches_manual_entry_data(self):
        resp = self.client.post(self.url, {
            'gvg_record_id': self.rec.pk, 'odometer_reading': 85000, 'state': 'QLD',
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        cv = CurrentVehicle.objects.get(user=self.user)
        med = cv.manual_entry_data
        self.assertEqual(med['brand'], 'Toyota')
        self.assertEqual(med['year'], 2019)
        self.assertEqual(med['fuel_consumption_per_100km'], 6.0)
        self.assertEqual(med['source'], 'gvg')  # provenance travels with the data
        self.assertEqual(med['source_dataset'], GVGSourceDataset.CURRENT)
        self.assertIsNone(cv.vehicle_catalogue_id)

    def test_user_values_override_gvg(self):
        resp = self.client.post(self.url, {
            'gvg_record_id': self.rec.pk,
            'manual_entry_data': {'fuel_consumption_per_100km': 7.2},
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        cv = CurrentVehicle.objects.get(user=self.user)
        self.assertEqual(cv.manual_entry_data['fuel_consumption_per_100km'], 7.2)
        self.assertEqual(cv.manual_entry_data['brand'], 'Toyota')

    def test_unknown_gvg_record_rejected(self):
        resp = self.client.post(self.url, {'gvg_record_id': 999999}, format='json')
        self.assertEqual(resp.status_code, 400)
