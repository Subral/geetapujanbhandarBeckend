import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
import bcrypt
import uuid
from datetime import datetime, timezone
import os
from dotenv import load_dotenv
from pathlib import Path

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

async def seed_database():
    print("Seeding database...")
    
    admin_password = bcrypt.hashpw('admin123'.encode('utf-8'), bcrypt.gensalt())
    admin_id = str(uuid.uuid4())
    
    existing_admin = await db.users.find_one({"email": "admin@geetapujan.com"})
    if not existing_admin:
        admin_user = {
            "id": admin_id,
            "name": "Admin",
            "email": "admin@geetapujan.com",
            "password": admin_password.decode('utf-8'),
            "phone": "9876543210",
            "role": "admin",
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        await db.users.insert_one(admin_user)
        print("✓ Admin user created (email: admin@geetapujan.com, password: admin123)")
    else:
        print("✓ Admin user already exists")
    
    categories = [
        {
            "id": str(uuid.uuid4()),
            "name": "Krishna",
            "type": "deity",
            "image": "https://images.unsplash.com/photo-1661619669807-784e46af8029?q=85",
            "created_at": datetime.now(timezone.utc).isoformat()
        },
        {
            "id": str(uuid.uuid4()),
            "name": "Hanuman",
            "type": "deity",
            "image": "https://images.unsplash.com/photo-1712453257076-6602d66bd6e6?q=85",
            "created_at": datetime.now(timezone.utc).isoformat()
        },
        {
            "id": str(uuid.uuid4()),
            "name": "Shiva",
            "type": "deity",
            "image": "https://images.unsplash.com/photo-1759641914851-2be25e8cb31c?q=85",
            "created_at": datetime.now(timezone.utc).isoformat()
        },
        {
            "id": str(uuid.uuid4()),
            "name": "Korean Marble",
            "type": "material",
            "image": "https://images.unsplash.com/photo-1731922910212-507d1e944dea?q=85",
            "created_at": datetime.now(timezone.utc).isoformat()
        }
    ]
    
    existing_categories = await db.categories.count_documents({})
    if existing_categories == 0:
        await db.categories.insert_many(categories)
        print(f"✓ Created {len(categories)} categories")
    else:
        print(f"✓ {existing_categories} categories already exist")
    
    products = [
        {
            "id": str(uuid.uuid4()),
            "name": "Divine Krishna Ji Statue",
            "description": "Beautiful handcrafted Krishna statue in pure Korean marble. Perfect for home temple and daily worship.",
            "deity": "Krishna",
            "material": "Korean Marble",
            "price": 5999,
            "image": "https://images.unsplash.com/photo-1661619669807-784e46af8029?q=85",
            "images": [],
            "stock": 25,
            "category": "statue",
            "weight": "2.5 kg",
            "dimensions": "12 x 6 x 18 inches",
            "created_at": datetime.now(timezone.utc).isoformat()
        },
        {
            "id": str(uuid.uuid4()),
            "name": "Hanuman Ji Brass Idol",
            "description": "Premium brass Hanuman idol with antique finish. Blessed for protection and strength.",
            "deity": "Hanuman",
            "material": "Brass",
            "price": 3499,
            "image": "https://images.unsplash.com/photo-1712453257076-6602d66bd6e6?q=85",
            "images": [],
            "stock": 30,
            "category": "statue",
            "weight": "1.8 kg",
            "dimensions": "10 x 5 x 15 inches",
            "created_at": datetime.now(timezone.utc).isoformat()
        },
        {
            "id": str(uuid.uuid4()),
            "name": "Shiva Lingam Red Marble",
            "description": "Sacred Shiva Lingam carved from authentic red marble. Ideal for daily abhishekam.",
            "deity": "Shiva",
            "material": "Red Marble",
            "price": 2999,
            "image": "https://images.unsplash.com/photo-1759641914851-2be25e8cb31c?q=85",
            "images": [],
            "stock": 40,
            "category": "statue",
            "weight": "3 kg",
            "dimensions": "8 x 8 x 10 inches",
            "created_at": datetime.now(timezone.utc).isoformat()
        },
        {
            "id": str(uuid.uuid4()),
            "name": "Krishna Ji Copper Idol",
            "description": "Exquisite copper Krishna idol with fine detailing. Pure copper for spiritual benefits.",
            "deity": "Krishna",
            "material": "Copper",
            "price": 4299,
            "image": "https://images.unsplash.com/photo-1661619669807-784e46af8029?q=85",
            "images": [],
            "stock": 20,
            "category": "statue",
            "weight": "1.5 kg",
            "dimensions": "10 x 5 x 14 inches",
            "created_at": datetime.now(timezone.utc).isoformat()
        },
        {
            "id": str(uuid.uuid4()),
            "name": "Vishnu Ji Korean Marble",
            "description": "Majestic Vishnu statue in premium Korean marble. Perfect for Vishnu devotees.",
            "deity": "Vishnu",
            "material": "Korean Marble",
            "price": 6999,
            "image": "https://images.unsplash.com/photo-1731922910212-507d1e944dea?q=85",
            "images": [],
            "stock": 15,
            "category": "statue",
            "weight": "3.2 kg",
            "dimensions": "14 x 7 x 20 inches",
            "created_at": datetime.now(timezone.utc).isoformat()
        },
        {
            "id": str(uuid.uuid4()),
            "name": "Hanuman Chalisa Copper Frame",
            "description": "Beautifully engraved Hanuman Chalisa on copper frame. Wall hanging.",
            "deity": "Hanuman",
            "material": "Copper",
            "price": 1299,
            "image": "https://images.unsplash.com/photo-1712453257076-6602d66bd6e6?q=85",
            "images": [],
            "stock": 50,
            "category": "accessory",
            "weight": "0.5 kg",
            "dimensions": "12 x 18 inches",
            "created_at": datetime.now(timezone.utc).isoformat()
        }
    ]
    
    existing_products = await db.products.count_documents({})
    if existing_products == 0:
        await db.products.insert_many(products)
        print(f"✓ Created {len(products)} sample products")
    else:
        print(f"✓ {existing_products} products already exist")
    
    offers = [
        {
            "id": str(uuid.uuid4()),
            "title": "New Year Special",
            "description": "Flat 20% OFF on all Krishna Statues",
            "code": "KRISHNA20",
            "image": "https://images.unsplash.com/photo-1661619669807-784e46af8029?q=85",
            "bg_color": "linear-gradient(135deg, #667eea 0%, #764ba2 100%)",
            "active": True,
            "created_at": datetime.now(timezone.utc).isoformat()
        },
        {
            "id": str(uuid.uuid4()),
            "title": "Festival Offer",
            "description": "Buy 2 Get 1 Free on Pooja Items",
            "code": "POOJA3",
            "image": "https://images.unsplash.com/photo-1589095053205-8fc842336f4a?q=85",
            "bg_color": "linear-gradient(135deg, #f093fb 0%, #f5576c 100%)",
            "active": True,
            "created_at": datetime.now(timezone.utc).isoformat()
        },
        {
            "id": str(uuid.uuid4()),
            "title": "Premium Collection",
            "description": "Exclusive Korean Marble - ₹500 OFF",
            "code": "MARBLE500",
            "image": "https://images.unsplash.com/photo-1731922910212-507d1e944dea?q=85",
            "bg_color": "linear-gradient(135deg, #4facfe 0%, #00f2fe 100%)",
            "active": True,
            "created_at": datetime.now(timezone.utc).isoformat()
        },
        {
            "id": str(uuid.uuid4()),
            "title": "Free Delivery",
            "description": "No delivery charges on orders above ₹999",
            "code": "FREEDEL",
            "image": "https://images.unsplash.com/photo-1712453257076-6602d66bd6e6?q=85",
            "bg_color": "linear-gradient(135deg, #43e97b 0%, #38f9d7 100%)",
            "active": True,
            "created_at": datetime.now(timezone.utc).isoformat()
        }
    ]
    
    existing_offers = await db.offers.count_documents({})
    if existing_offers == 0:
        await db.offers.insert_many(offers)
        print(f"✓ Created {len(offers)} sample offers")
    else:
        print(f"✓ {existing_offers} offers already exist")
    
    print("\nDatabase seeding completed!")
    print("\nCredentials:")
    print("Admin Login: admin@geetapujan.com / admin123")

if __name__ == "__main__":
    asyncio.run(seed_database())
    client.close()
