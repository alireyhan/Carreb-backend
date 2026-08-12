"""Pinned regression tests for the CORE rating engine.

Locks the SPEC §4/E2 fixed-band method (percentile/quintile mapping is PROHIBITED) so a
future change can't silently reintroduce the percentile approach or a wrong formula.
"""
from django.test import TestCase

from apps.vehicles.models import PowertrainType
from apps.vehicles.services import rating_engine
from apps.vehicles.services.rating_engine import core_rating_from_scores, CORE_MODE_TO_KEY


class CoreRatingFixedBandTests(TestCase):
    def test_default_weights_no_cap(self):
        # 80/20, price_score 0.8, emissions 0.6, petrol, co2 120 (< 150 -> no cap)
        # core_score = 0.8*0.8 + 0.2*0.6 = 0.76 -> rating = round(3.8, 1) = 3.8
        capped, uncapped, cap = core_rating_from_scores(0.8, 0.6, PowertrainType.PETROL, 120, 0.8, 0.2)
        self.assertEqual(uncapped, 3.8)
        self.assertIsNone(cap)
        self.assertEqual(capped, 3.8)

    def test_slider_reweights_stored_scores(self):
        # Pure emissions weight (slider pos 5): rating tracks emissions_score only.
        capped, uncapped, cap = core_rating_from_scores(0.2, 1.0, PowertrainType.HYBRID, 100, 0.0, 1.0)
        self.assertEqual(uncapped, 5.0)  # 0.0*0.2 + 1.0*1.0 = 1.0 -> 5.0
        self.assertEqual(capped, 5.0)

    def test_co2_star_cap_applied(self):
        # High score but 260 g/km -> capped at 2.4 stars regardless of cost.
        capped, uncapped, cap = core_rating_from_scores(1.0, 1.0, PowertrainType.PETROL, 260, 0.8, 0.2)
        self.assertEqual(uncapped, 5.0)
        self.assertEqual(cap, 2.4)
        self.assertEqual(capped, 2.4)

    def test_co2_cap_tiers(self):
        self.assertEqual(core_rating_from_scores(1.0, 1.0, PowertrainType.PETROL, 200, 0.8, 0.2)[2], 3.4)
        self.assertEqual(core_rating_from_scores(1.0, 1.0, PowertrainType.PETROL, 150, 0.8, 0.2)[2], 4.4)
        self.assertIsNone(core_rating_from_scores(1.0, 1.0, PowertrainType.PETROL, 149, 0.8, 0.2)[2])

    def test_bev_emissions_hard_set_and_never_capped(self):
        # BEV: emissions_score forced to 1.0 and never capped even at a high co2 input.
        capped, uncapped, cap = core_rating_from_scores(0.5, 0.0, PowertrainType.BEV, 300, 0.8, 0.2)
        # core = 0.8*0.5 + 0.2*1.0 = 0.6 -> 3.0 ; BEV never capped
        self.assertEqual(uncapped, 3.0)
        self.assertIsNone(cap)
        self.assertEqual(capped, 3.0)

    def test_cap_never_raises_a_rating(self):
        # A low rating below the cap threshold is unchanged by the cap.
        capped, uncapped, cap = core_rating_from_scores(0.3, 0.3, PowertrainType.PETROL, 260, 0.8, 0.2)
        self.assertEqual(uncapped, 1.5)   # 0.3 -> 1.5
        self.assertEqual(cap, 2.4)
        self.assertEqual(capped, 1.5)     # min(1.5, 2.4) = 1.5, not raised

    def test_mode_key_mapping_matches_spec(self):
        # B=Pure Cost, E=COOL Choice, C=True Value, F=Full Picture (spec §4).
        self.assertEqual(CORE_MODE_TO_KEY['pure_cost'], 'B')
        self.assertEqual(CORE_MODE_TO_KEY['cool_choice'], 'E')
        self.assertEqual(CORE_MODE_TO_KEY['true_value'], 'C')
        self.assertEqual(CORE_MODE_TO_KEY['full_picture'], 'F')

    def test_prohibited_percentile_function_removed(self):
        self.assertFalse(hasattr(rating_engine, 'get_percentile_score'))
