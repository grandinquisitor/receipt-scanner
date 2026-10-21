import cv2
import numpy as np
import pytest

from receipt_scanner import imaging

W, H = 1000, 1400


def photo(corners, paper=235, table=60, light=0, noise=6, seed=0) -> np.ndarray:
    """A printed receipt with corners `corners` (TL, TR, BR, BL) lying on a plain surface, with lighting
    that brightens by `light` grey levels from the top-left to the bottom-right corner."""
    rng = np.random.default_rng(seed)
    img = np.full((H, W), table, np.float32)
    cv2.fillConvexPoly(img, np.array(corners, np.int32), paper)
    # some "printed lines" across the receipt, so it isn't a blank card
    m = cv2.getPerspectiveTransform(np.float32([[0, 0], [1, 0], [1, 1], [0, 1]]), np.float32(corners))
    for y in np.linspace(0.1, 0.9, 12):
        a, b = cv2.perspectiveTransform(np.float32([[[0.15, y], [0.75, y]]]), m)[0]
        cv2.line(img, tuple(map(int, a)), tuple(map(int, b)), 50, 6)
    yy, xx = np.mgrid[0:H, 0:W]
    img += light * (xx / W + yy / H - 1)
    return np.clip(img + rng.normal(0, noise, img.shape), 0, 255).astype(np.uint8)


def assert_corners_near(quad, corners, tol=0.03):
    assert quad is not None, "no receipt found"
    err = np.abs(np.asarray(quad) - np.float32(corners)).max()
    assert err < tol * max(W, H), f"corners off by {err:.0f}px:\n{quad}"


@pytest.mark.parametrize("corners", [
    [(300, 200), (700, 200), (700, 1250), (300, 1250)],  # straight
    [(330, 150), (720, 230), (640, 1280), (240, 1200)],  # tilted
    [(380, 260), (640, 240), (760, 1220), (250, 1260)],  # perspective (shot at an angle)
])
def test_finds_the_receipt(corners):
    assert_corners_near(imaging.find_receipt_quad(photo(corners)), corners)


@pytest.mark.parametrize("seed", range(3))
def test_finds_a_receipt_on_a_white_sheet(seed):
    """Too little contrast for the brightness and edge masks; only the straight-edge fallback finds it."""
    corners = [(330, 150), (720, 230), (640, 1280), (240, 1200)]
    grey = photo(corners, paper=235, table=215, light=30, noise=4, seed=seed)
    assert_corners_near(imaging.find_receipt_quad(grey), corners)


def test_scan_is_cropped_to_the_receipt():
    corners = [(300, 200), (700, 200), (700, 1250), (300, 1250)]
    scan = imaging.scan_from_grey(photo(corners))
    assert scan is not None
    h, w = cv2.imdecode(np.frombuffer(scan, np.uint8), cv2.IMREAD_GRAYSCALE).shape
    assert abs(h / w - 1050 / 400) < 0.15  # same shape as the receipt
    assert max(h, w) <= imaging.MAX_EDGE


def test_no_receipt_means_no_scan():
    rng = np.random.default_rng(1)
    empty_table = np.clip(rng.normal(90, 6, (H, W)), 0, 255).astype(np.uint8)
    assert imaging.scan_from_grey(empty_table) is None
