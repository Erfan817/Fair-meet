import pytest

from fairmeet.center import geographic_center


def test_single_point_returns_itself():
    lng, lat = geographic_center([(116.31, 39.99)])
    assert lng == pytest.approx(116.31, abs=1e-6)
    assert lat == pytest.approx(39.99, abs=1e-6)


def test_order_does_not_matter():
    points_a = [(116.31, 39.99), (121.47, 31.23), (113.26, 23.13)]
    points_b = [(113.26, 23.13), (116.31, 39.99), (121.47, 31.23)]  # 同样的点，打乱顺序
    result_a = geographic_center(points_a)
    result_b = geographic_center(points_b)
    assert result_a[0] == pytest.approx(result_b[0], abs=1e-6)
    assert result_a[1] == pytest.approx(result_b[1], abs=1e-6)


def test_symmetric_points():
    lng, lat = geographic_center([(115.0, 0.0), (117.0, 0.0)])
    assert lng == pytest.approx(116.0, abs=1e-4)
    assert lat == pytest.approx(0.0, abs=1e-4)


def test_empty_list_raises():
    with pytest.raises(ValueError):
        geographic_center([])
