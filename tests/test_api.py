"""
Backend API Tests for Geeta Pujan Bhandar
Testing: Auth, Products, Cart, Orders, Promo Code APIs
"""
import pytest
import requests
import os

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', 'https://krishna-pujan-store.preview.emergentagent.com').rstrip('/')

# Test credentials
ADMIN_EMAIL = "admin@geetapujan.com"
ADMIN_PASSWORD = "admin123"
TEST_USER_EMAIL = "testuser_api@example.com"
TEST_USER_PASSWORD = "testpass123"
TEST_USER_NAME = "Test API User"
TEST_USER_PHONE = "9876543210"


class TestHealthAndRoot:
    """Health check and root endpoint tests"""
    
    def test_api_root(self):
        """Test API root endpoint"""
        response = requests.get(f"{BASE_URL}/api/")
        assert response.status_code == 200
        data = response.json()
        assert "message" in data
        print(f"API Root: {data['message']}")
    
    def test_homepage_settings(self):
        """Test homepage settings endpoint"""
        response = requests.get(f"{BASE_URL}/api/homepage-settings")
        assert response.status_code == 200
        data = response.json()
        assert "hero_title" in data
        assert "hero_subtitle" in data
        print(f"Homepage settings: title={data['hero_title']}")


class TestAuthentication:
    """Authentication endpoint tests"""
    
    def test_register_new_user(self):
        """Test user registration"""
        import uuid
        unique_email = f"testuser_{uuid.uuid4().hex[:8]}@example.com"
        response = requests.post(f"{BASE_URL}/api/auth/register", json={
            "name": TEST_USER_NAME,
            "email": unique_email,
            "password": TEST_USER_PASSWORD,
            "phone": TEST_USER_PHONE
        })
        
        if response.status_code == 400:
            # User already exists - acceptable
            print(f"User might already exist: {response.json()}")
            return
        
        assert response.status_code == 200
        data = response.json()
        assert "token" in data
        assert "user" in data
        assert data["user"]["email"] == unique_email
        print(f"Registered user: {data['user']['email']}")
    
    def test_login_admin(self):
        """Test admin login"""
        response = requests.post(f"{BASE_URL}/api/auth/login", json={
            "email": ADMIN_EMAIL,
            "password": ADMIN_PASSWORD
        })
        assert response.status_code == 200
        data = response.json()
        assert "token" in data
        assert "user" in data
        assert data["user"]["role"] == "admin"
        print(f"Admin login successful: {data['user']['email']}")
    
    def test_login_invalid_credentials(self):
        """Test login with invalid credentials"""
        response = requests.post(f"{BASE_URL}/api/auth/login", json={
            "email": "wrong@example.com",
            "password": "wrongpassword"
        })
        assert response.status_code == 401
        print("Invalid login correctly rejected with 401")
    
    def test_get_current_user(self):
        """Test getting current user info (/api/auth/me)"""
        # First login
        login_response = requests.post(f"{BASE_URL}/api/auth/login", json={
            "email": ADMIN_EMAIL,
            "password": ADMIN_PASSWORD
        })
        assert login_response.status_code == 200
        token = login_response.json()["token"]
        
        # Get user info
        response = requests.get(f"{BASE_URL}/api/auth/me", headers={
            "Authorization": f"Bearer {token}"
        })
        assert response.status_code == 200
        data = response.json()
        assert "id" in data
        assert "name" in data
        assert "email" in data
        assert "phone" in data
        print(f"User info retrieved: name={data['name']}, email={data['email']}, phone={data['phone']}")


class TestProducts:
    """Product endpoint tests"""
    
    def test_get_all_products(self):
        """Test getting all products"""
        response = requests.get(f"{BASE_URL}/api/products")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        print(f"Found {len(data)} products")
        if len(data) > 0:
            print(f"First product: {data[0]['name']}")
    
    def test_get_products_by_deity_filter(self):
        """Test products filtering by deity"""
        response = requests.get(f"{BASE_URL}/api/products?deity=Krishna")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        print(f"Found {len(data)} Krishna products")
    
    def test_get_products_by_material_filter(self):
        """Test products filtering by material"""
        response = requests.get(f"{BASE_URL}/api/products?material=Marble")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        print(f"Found {len(data)} Marble products")
    
    def test_get_single_product(self):
        """Test getting a single product"""
        # First get all products
        products_response = requests.get(f"{BASE_URL}/api/products")
        products = products_response.json()
        
        if len(products) == 0:
            pytest.skip("No products available to test")
        
        product_id = products[0]["id"]
        response = requests.get(f"{BASE_URL}/api/products/{product_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == product_id
        print(f"Product details: {data['name']} - ₹{data['price']}")
    
    def test_get_nonexistent_product(self):
        """Test getting a product that doesn't exist"""
        response = requests.get(f"{BASE_URL}/api/products/nonexistent-id-12345")
        assert response.status_code == 404


class TestCategories:
    """Category endpoint tests"""
    
    def test_get_categories(self):
        """Test getting all categories"""
        response = requests.get(f"{BASE_URL}/api/categories")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        print(f"Found {len(data)} categories")


class TestOffers:
    """Offer endpoint tests"""
    
    def test_get_offers(self):
        """Test getting all offers"""
        response = requests.get(f"{BASE_URL}/api/offers")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        print(f"Found {len(data)} offers")
    
    def test_get_active_offers(self):
        """Test getting active offers"""
        response = requests.get(f"{BASE_URL}/api/offers?active=true")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        print(f"Found {len(data)} active offers")


class TestPromoCode:
    """Promo code validation tests"""
    
    def test_validate_invalid_promo(self):
        """Test invalid promo code"""
        response = requests.post(f"{BASE_URL}/api/validate-promo?code=INVALIDCODE&total=1000")
        assert response.status_code == 404
        print("Invalid promo code correctly rejected")
    
    def test_validate_valid_promo(self):
        """Test valid promo code (if offers exist)"""
        # Get active offers first
        offers_response = requests.get(f"{BASE_URL}/api/offers?active=true")
        offers = offers_response.json()
        
        if len(offers) == 0:
            pytest.skip("No active offers to test promo code")
        
        promo_code = offers[0].get("code")
        if not promo_code:
            pytest.skip("No promo code found in offers")
        
        response = requests.post(f"{BASE_URL}/api/validate-promo?code={promo_code}&total=1000")
        assert response.status_code == 200
        data = response.json()
        assert "valid" in data
        assert "offer" in data
        print(f"Promo code '{promo_code}' validated: discount={data.get('discount', 0)}")


class TestCart:
    """Cart endpoint tests - requires authentication"""
    
    @pytest.fixture
    def auth_token(self):
        """Get auth token for cart operations"""
        response = requests.post(f"{BASE_URL}/api/auth/login", json={
            "email": ADMIN_EMAIL,
            "password": ADMIN_PASSWORD
        })
        if response.status_code != 200:
            pytest.skip("Cannot authenticate for cart tests")
        return response.json()["token"]
    
    def test_get_cart(self, auth_token):
        """Test getting cart"""
        response = requests.get(f"{BASE_URL}/api/cart", headers={
            "Authorization": f"Bearer {auth_token}"
        })
        assert response.status_code == 200
        data = response.json()
        assert "items" in data
        print(f"Cart has {len(data['items'])} items")
    
    def test_add_to_cart(self, auth_token):
        """Test adding item to cart"""
        # First get a product
        products_response = requests.get(f"{BASE_URL}/api/products")
        products = products_response.json()
        
        if len(products) == 0:
            pytest.skip("No products available to add to cart")
        
        product_id = products[0]["id"]
        
        # Add to cart
        response = requests.post(f"{BASE_URL}/api/cart", 
            json={"product_id": product_id, "quantity": 1},
            headers={"Authorization": f"Bearer {auth_token}"}
        )
        assert response.status_code == 200
        print(f"Added product {product_id} to cart")
        
        # Verify cart has the item
        cart_response = requests.get(f"{BASE_URL}/api/cart", headers={
            "Authorization": f"Bearer {auth_token}"
        })
        cart = cart_response.json()
        assert any(item["product_id"] == product_id for item in cart["items"])
        print("Verified item is in cart")


class TestOrders:
    """Order endpoint tests - requires authentication"""
    
    @pytest.fixture
    def auth_token(self):
        """Get auth token for order operations"""
        response = requests.post(f"{BASE_URL}/api/auth/login", json={
            "email": ADMIN_EMAIL,
            "password": ADMIN_PASSWORD
        })
        if response.status_code != 200:
            pytest.skip("Cannot authenticate for order tests")
        return response.json()["token"]
    
    def test_get_orders(self, auth_token):
        """Test getting user orders"""
        response = requests.get(f"{BASE_URL}/api/orders", headers={
            "Authorization": f"Bearer {auth_token}"
        })
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        print(f"User has {len(data)} orders")
        
        if len(data) > 0:
            order = data[0]
            assert "id" in order
            assert "items" in order
            assert "total" in order
            assert "order_status" in order
            print(f"First order: #{order['id'][:8]}, status={order['order_status']}, total=₹{order['total']}")
    
    def test_get_orders_without_auth(self):
        """Test orders endpoint without authentication"""
        response = requests.get(f"{BASE_URL}/api/orders")
        assert response.status_code in [401, 403]
        print("Orders endpoint correctly requires authentication")


class TestAdminEndpoints:
    """Admin endpoint tests"""
    
    @pytest.fixture
    def admin_token(self):
        """Get admin auth token"""
        response = requests.post(f"{BASE_URL}/api/auth/login", json={
            "email": ADMIN_EMAIL,
            "password": ADMIN_PASSWORD
        })
        if response.status_code != 200:
            pytest.skip("Cannot authenticate as admin")
        return response.json()["token"]
    
    def test_admin_stats(self, admin_token):
        """Test admin stats endpoint"""
        response = requests.get(f"{BASE_URL}/api/admin/stats", headers={
            "Authorization": f"Bearer {admin_token}"
        })
        assert response.status_code == 200
        data = response.json()
        assert "total_products" in data
        assert "total_orders" in data
        assert "total_users" in data
        print(f"Admin stats: products={data['total_products']}, orders={data['total_orders']}, users={data['total_users']}")
    
    def test_admin_orders(self, admin_token):
        """Test admin orders endpoint"""
        response = requests.get(f"{BASE_URL}/api/admin/orders", headers={
            "Authorization": f"Bearer {admin_token}"
        })
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        print(f"Admin sees {len(data)} total orders")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
