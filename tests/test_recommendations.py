"""
Backend API tests for the AI Recommendation feature.
Covers: interaction tracking, batch tracking, recommendations,
trending products, similar products, and interaction history.
"""
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://krishna-pujan-store.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"

ADMIN_EMAIL = "admin@geetapujan.com"
ADMIN_PASSWORD = "admin123"


# --- Fixtures ---
@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def auth_token(session):
    r = session.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    if r.status_code != 200:
        pytest.skip(f"Login failed: {r.status_code} {r.text}")
    data = r.json()
    token = data.get("token") or data.get("access_token")
    if not token:
        pytest.skip(f"No token in login response: {data}")
    return token


@pytest.fixture(scope="module")
def auth_session(session, auth_token):
    session.headers.update({"Authorization": f"Bearer {auth_token}"})
    return session


@pytest.fixture(scope="module")
def sample_product_ids(session):
    r = session.get(f"{API}/products?limit=5")
    assert r.status_code == 200, f"Products list failed: {r.text}"
    products = r.json()
    if isinstance(products, dict):
        products = products.get("products", [])
    assert len(products) > 0, "No products available for testing"
    return [p["id"] for p in products[:3]]


# --- Health/products sanity ---
class TestProductsAvailability:
    def test_products_endpoint_works(self, session):
        r = session.get(f"{API}/products?limit=1")
        assert r.status_code == 200


# --- Tracking ---
class TestTracking:
    def test_track_interaction_view(self, auth_session, sample_product_ids):
        r = auth_session.post(
            f"{API}/tracking/interaction",
            json={"product_id": sample_product_ids[0], "interaction_type": "view"},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data.get("status") == "tracked"
        assert "interaction_id" in data and isinstance(data["interaction_id"], str)

    def test_track_interaction_add_to_cart(self, auth_session, sample_product_ids):
        r = auth_session.post(
            f"{API}/tracking/interaction",
            json={"product_id": sample_product_ids[1], "interaction_type": "add_to_cart"},
        )
        assert r.status_code == 200
        assert r.json().get("status") == "tracked"

    def test_track_interaction_purchase(self, auth_session, sample_product_ids):
        r = auth_session.post(
            f"{API}/tracking/interaction",
            json={"product_id": sample_product_ids[2], "interaction_type": "purchase"},
        )
        assert r.status_code == 200
        assert r.json().get("status") == "tracked"

    def test_track_interaction_invalid_product(self, auth_session):
        r = auth_session.post(
            f"{API}/tracking/interaction",
            json={"product_id": "non-existent-id", "interaction_type": "view"},
        )
        assert r.status_code == 404

    def test_track_interaction_unauthenticated(self, session, sample_product_ids):
        clean = requests.Session()
        clean.headers.update({"Content-Type": "application/json"})
        r = clean.post(
            f"{API}/tracking/interaction",
            json={"product_id": sample_product_ids[0], "interaction_type": "view"},
        )
        assert r.status_code in (401, 403)

    def test_batch_tracking(self, auth_session, sample_product_ids):
        payload = [
            {"product_id": sample_product_ids[0], "interaction_type": "view"},
            {"product_id": sample_product_ids[1], "interaction_type": "view"},
            {"product_id": sample_product_ids[2], "interaction_type": "view"},
        ]
        r = auth_session.post(f"{API}/tracking/batch", json=payload)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data.get("status") == "tracked"
        assert data.get("count") == 3


# --- Recommendations ---
class TestRecommendations:
    def test_personalized_recommendations(self, auth_session):
        r = auth_session.get(f"{API}/recommendations?limit=5")
        assert r.status_code == 200, r.text
        data = r.json()
        assert "recommendations" in data
        assert isinstance(data["recommendations"], list)
        assert "is_personalized" in data
        assert "recommendation_type" in data
        assert data["recommendation_type"] in ("personalized", "trending")
        # Admin has logged interactions in earlier tests, so should be personalized
        if data["user_interaction_count"] > 0:
            assert data["is_personalized"] is True
            assert data["recommendation_type"] == "personalized"
        # Each recommendation should have a product id and score
        for rec in data["recommendations"]:
            assert "id" in rec
            assert "recommendation_score" in rec

    def test_recommendations_unauthenticated(self):
        r = requests.get(f"{API}/recommendations?limit=5")
        assert r.status_code in (401, 403)

    def test_trending_no_auth(self):
        r = requests.get(f"{API}/recommendations/trending?limit=5")
        assert r.status_code == 200, r.text
        data = r.json()
        assert "products" in data
        assert "source" in data
        assert data["source"] in ("trending", "latest")
        assert isinstance(data["products"], list)

    def test_similar_products(self, session, sample_product_ids):
        r = session.get(f"{API}/recommendations/similar/{sample_product_ids[0]}?limit=4")
        assert r.status_code == 200, r.text
        data = r.json()
        assert "products" in data
        assert "source" in data
        assert data["source"] in ("ml_similarity", "category_fallback", "none")
        # Returned product should not include the input product id
        for p in data["products"]:
            assert p.get("id") != sample_product_ids[0]

    def test_similar_products_invalid_id(self, session):
        r = session.get(f"{API}/recommendations/similar/non-existent-id?limit=4")
        assert r.status_code == 200
        data = r.json()
        assert data.get("source") == "none"
        assert data.get("products") == []


# --- Interaction History ---
class TestInteractionHistory:
    def test_get_interaction_history(self, auth_session):
        r = auth_session.get(f"{API}/user/interaction-history?limit=20")
        assert r.status_code == 200, r.text
        data = r.json()
        assert "interactions" in data
        assert "count" in data
        assert isinstance(data["interactions"], list)
        # Admin tracked interactions earlier, so history should be > 0
        assert data["count"] > 0
        # Validate structure of an interaction document
        sample = data["interactions"][0]
        for key in ("product_id", "interaction_type", "user_id", "created_at"):
            assert key in sample

    def test_history_unauthenticated(self):
        r = requests.get(f"{API}/user/interaction-history")
        assert r.status_code in (401, 403)
