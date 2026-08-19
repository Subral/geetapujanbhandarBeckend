"""
Banner API Tests - Public + Admin CRUD
"""
import pytest
import requests
import os

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL').rstrip('/')
ADMIN_EMAIL = "admin@geetapujan.com"
ADMIN_PASSWORD = "admin123"


@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(f"{BASE_URL}/api/auth/login", json={
        "email": ADMIN_EMAIL, "password": ADMIN_PASSWORD
    })
    assert r.status_code == 200, f"Admin login failed: {r.text}"
    return r.json()["token"]


@pytest.fixture(scope="module")
def customer_token():
    import uuid
    email = f"testuser_banner_{uuid.uuid4().hex[:8]}@example.com"
    r = requests.post(f"{BASE_URL}/api/auth/register", json={
        "name": "TestBannerUser", "email": email,
        "password": "testpass123", "phone": "9999999999"
    })
    if r.status_code == 200:
        return r.json()["token"]
    pytest.skip("Customer registration failed")


class TestBannerPublic:
    def test_get_active_banners_no_auth(self):
        r = requests.get(f"{BASE_URL}/api/banners")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        # All returned banners must be active
        for b in data:
            assert b.get("is_active") is True
            assert "_id" not in b
            assert "id" in b
            assert "title" in b
            assert "image_url" in b
            assert "target_link" in b
            assert "display_order" in b
        # Verify sorted by display_order asc
        orders = [b["display_order"] for b in data]
        assert orders == sorted(orders), f"Banners not sorted by display_order: {orders}"
        print(f"Public banners count: {len(data)}; orders={orders}")


class TestBannerAdmin:
    def test_get_all_banners_requires_admin(self):
        r = requests.get(f"{BASE_URL}/api/admin/banners")
        assert r.status_code in (401, 403)

    def test_create_banner_requires_admin(self):
        r = requests.post(f"{BASE_URL}/api/admin/banners", json={
            "title": "TEST_NoAuth", "image_url": "https://x.com/i.jpg",
            "target_link": "/x", "display_order": 99, "is_active": True
        })
        assert r.status_code in (401, 403)

    def test_customer_cannot_create_banner(self, customer_token):
        r = requests.post(
            f"{BASE_URL}/api/admin/banners",
            json={"title": "TEST_Customer", "image_url": "https://x.com/i.jpg",
                  "target_link": "/x", "display_order": 99, "is_active": True},
            headers={"Authorization": f"Bearer {customer_token}"}
        )
        assert r.status_code == 403

    def test_get_all_banners_admin(self, admin_token):
        r = requests.get(f"{BASE_URL}/api/admin/banners",
                         headers={"Authorization": f"Bearer {admin_token}"})
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        for b in data:
            assert "_id" not in b
            assert "id" in b

    def test_banner_full_crud_flow(self, admin_token):
        headers = {"Authorization": f"Bearer {admin_token}"}
        # CREATE
        payload = {
            "title": "TEST_CRUD_Banner",
            "image_url": "https://example.com/banner.jpg",
            "target_link": "/products/test",
            "display_order": 50,
            "is_active": True
        }
        r = requests.post(f"{BASE_URL}/api/admin/banners", json=payload, headers=headers)
        assert r.status_code == 200, f"Create failed: {r.status_code} {r.text}"
        created = r.json()
        assert created["title"] == payload["title"]
        assert created["image_url"] == payload["image_url"]
        assert created["target_link"] == payload["target_link"]
        assert created["display_order"] == 50
        assert created["is_active"] is True
        assert "id" in created
        assert "_id" not in created
        banner_id = created["id"]

        # Verify via GET admin list
        r = requests.get(f"{BASE_URL}/api/admin/banners", headers=headers)
        assert r.status_code == 200
        assert any(b["id"] == banner_id for b in r.json())

        # Verify public list contains it (since active)
        r = requests.get(f"{BASE_URL}/api/banners")
        assert any(b["id"] == banner_id for b in r.json())

        # UPDATE - toggle inactive + change title
        upd = {"title": "TEST_CRUD_Banner_Updated", "is_active": False}
        r = requests.put(f"{BASE_URL}/api/admin/banners/{banner_id}", json=upd, headers=headers)
        assert r.status_code == 200, f"Update failed: {r.status_code} {r.text}"
        updated = r.json()
        assert updated["title"] == "TEST_CRUD_Banner_Updated"
        assert updated["is_active"] is False
        assert updated["image_url"] == payload["image_url"]  # unchanged

        # Verify NOT in public list (inactive)
        r = requests.get(f"{BASE_URL}/api/banners")
        assert not any(b["id"] == banner_id for b in r.json()), "Inactive banner appearing in public list"

        # DELETE
        r = requests.delete(f"{BASE_URL}/api/admin/banners/{banner_id}", headers=headers)
        assert r.status_code == 200

        # Verify removed
        r = requests.get(f"{BASE_URL}/api/admin/banners", headers=headers)
        assert not any(b["id"] == banner_id for b in r.json())

    def test_update_nonexistent_banner(self, admin_token):
        r = requests.put(
            f"{BASE_URL}/api/admin/banners/nonexistent-id-xyz",
            json={"title": "x"},
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert r.status_code == 404

    def test_delete_nonexistent_banner(self, admin_token):
        r = requests.delete(
            f"{BASE_URL}/api/admin/banners/nonexistent-id-xyz",
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert r.status_code == 404


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
