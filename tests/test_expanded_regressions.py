import math
import unittest

from real_estate_engine import haversine_distance
from test_engine_regressions import memory_storage, property_offer, population_analyzer, LAT, LNG
from test_remaining_problems import building


class SphericalSearchTests(unittest.TestCase):
    def setUp(self):
        self.storage,self.conn=memory_storage()
        self.addCleanup(self.conn.close)

    def test_date_line_search_includes_both_sides(self):
        self.storage.upsert_batch([property_offer('east',lat=0,lng=179.9),property_offer('west',lat=0,lng=-179.9)])
        found=self.storage.find_within_radius_rtree(0,179.8,150000)
        self.assertEqual({r['external_id'] for r in found},{'east','west'})

    def test_circle_containing_pole_has_full_longitude_coverage(self):
        self.storage.upsert_property(property_offer(lat=89,lng=-150))
        self.assertLess(haversine_distance(85,30,89,-150),1000000)
        self.assertEqual(len(self.storage.find_within_radius_rtree(85,30,1000000)),1)

    def test_missing_coordinate_is_not_a_finite_distance(self):
        self.assertTrue(math.isinf(haversine_distance(None,0,0,0)))

    def test_antipodal_distance_remains_finite(self):
        for lat in (-89.2,-55.7,0,55.7,89.2):
            self.assertAlmostEqual(haversine_distance(lat,0,-lat,180),math.pi*6371000,delta=.2)

    def test_invalid_search_rejected(self):
        for lat,lng,radius in ((91,0,500),(0,181,500),(0,0,-1),(0,0,float('nan'))):
            with self.subTest(lat=lat,lng=lng,radius=radius),self.assertRaises(ValueError):
                self.storage.find_within_radius_rtree(lat,lng,radius)


class MalformedBuildingTests(unittest.TestCase):
    def test_invalid_records_do_not_destroy_valid_population(self):
        invalid=[None,[],"unknown",{"point":"unknown"},{"floors":"5"},
                 {"structure_info":"unknown"},{"links":{"database_entrances":"unknown"}},
                 {"geometry":{"hover":["POLYGON"]}}]
        for value in invalid:
            record={**building('bad'),**value} if isinstance(value,dict) else value
            with self.subTest(value=value):
                result=population_analyzer([building('valid'),record]).analyze_circle_sync(LAT,LNG)
                self.assertGreaterEqual(result.estimated_residents_count,201)
                self.assertGreater(result.malformed_buildings_count,0)
                self.assertTrue(any('формат' in w for w in result.data_warnings))

    def test_string_entrances_not_counted_as_character_count(self):
        record=building(links={'database_entrances':'unknown'})
        result=population_analyzer([record]).analyze_circle_sync(LAT,LNG)
        self.assertEqual(result.total_entrances_count,1)


if __name__=='__main__':
    unittest.main()
