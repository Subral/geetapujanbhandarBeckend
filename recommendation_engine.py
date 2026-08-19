"""
AI-Driven Recommendation Engine using Local ML (scikit-learn)

Strategy:
1. Content-based filtering using product attributes (category, material, deity)
2. Collaborative filtering based on user interaction patterns
3. Popularity-based fallback for cold start (new users)

No external API costs - pure local ML computation.
"""

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Tuple
import logging

logger = logging.getLogger(__name__)


class RecommendationEngine:
    """
    Hybrid recommendation engine combining:
    - Content-based filtering (product similarity)
    - Collaborative filtering (user behavior patterns)
    - Popularity-based recommendations (cold start fallback)
    """
    
    def __init__(self):
        self.vectorizer = TfidfVectorizer(stop_words='english', max_features=1000)
        self.product_vectors = None
        self.product_ids = []
        self.product_data = {}
        self.is_fitted = False
    
    def fit_products(self, products: List[dict]):
        """
        Build product feature vectors for content-based filtering.
        Combines name, description, category, material, and deity into a single text feature.
        """
        if not products:
            logger.warning("No products to fit")
            return
        
        self.product_ids = []
        self.product_data = {}
        product_texts = []
        
        for product in products:
            product_id = product.get('id')
            if not product_id:
                continue
                
            self.product_ids.append(product_id)
            self.product_data[product_id] = product
            
            # Create rich text representation of product
            text_features = [
                product.get('name', ''),
                product.get('description', ''),
                product.get('category', '') * 3,  # Weight category higher
                product.get('material', '') * 2,  # Weight material
                product.get('deity', '') * 2,     # Weight deity
            ]
            product_texts.append(' '.join(text_features))
        
        if product_texts:
            self.product_vectors = self.vectorizer.fit_transform(product_texts)
            self.is_fitted = True
            logger.info(f"Fitted recommendation engine with {len(self.product_ids)} products")
    
    def get_similar_products(self, product_id: str, n: int = 5) -> List[str]:
        """
        Find products similar to a given product using cosine similarity.
        """
        if not self.is_fitted or product_id not in self.product_data:
            return []
        
        try:
            idx = self.product_ids.index(product_id)
            product_vector = self.product_vectors[idx]
            
            # Calculate similarity scores
            similarities = cosine_similarity(product_vector, self.product_vectors).flatten()
            
            # Get top N similar products (excluding the product itself)
            similar_indices = similarities.argsort()[::-1][1:n+1]
            
            return [self.product_ids[i] for i in similar_indices if similarities[i] > 0.1]
        except Exception as e:
            logger.error(f"Error finding similar products: {e}")
            return []
    
    def calculate_user_preferences(self, interactions: List[dict]) -> Dict[str, float]:
        """
        Calculate user preference scores for different product attributes.
        
        Interaction weights:
        - purchase: 5.0 (strongest signal)
        - add_to_cart: 3.0
        - view: 1.0
        
        Time decay: Recent interactions weighted higher
        """
        preferences = defaultdict(float)
        
        # Interaction type weights
        weights = {
            'purchase': 5.0,
            'add_to_cart': 3.0,
            'view': 1.0
        }
        
        now = datetime.now(timezone.utc)
        
        for interaction in interactions:
            interaction_type = interaction.get('interaction_type', 'view')
            weight = weights.get(interaction_type, 1.0)
            
            # Time decay (interactions older than 30 days get reduced weight)
            created_at = interaction.get('created_at')
            if created_at:
                try:
                    if isinstance(created_at, str):
                        interaction_time = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                    else:
                        interaction_time = created_at
                    
                    days_old = (now - interaction_time).days
                    time_decay = max(0.3, 1.0 - (days_old / 60))  # Min 0.3 weight
                    weight *= time_decay
                except:
                    pass
            
            product_data = interaction.get('product_data', {})
            
            # Build preference scores for product attributes
            category = product_data.get('category', '')
            material = product_data.get('material', '')
            deity = product_data.get('deity', '')
            
            if category:
                preferences[f'category:{category}'] += weight
            if material:
                preferences[f'material:{material}'] += weight
            if deity:
                preferences[f'deity:{deity}'] += weight
        
        # Normalize preferences
        max_score = max(preferences.values()) if preferences else 1
        if max_score > 0:
            for key in preferences:
                preferences[key] /= max_score
        
        return dict(preferences)
    
    def score_product_for_user(self, product: dict, preferences: Dict[str, float]) -> float:
        """
        Score a product based on user preferences.
        """
        score = 0.0
        
        category = product.get('category', '')
        material = product.get('material', '')
        deity = product.get('deity', '')
        
        score += preferences.get(f'category:{category}', 0) * 1.5  # Category most important
        score += preferences.get(f'material:{material}', 0) * 1.0
        score += preferences.get(f'deity:{deity}', 0) * 1.2
        
        return score
    
    def get_content_based_recommendations(
        self, 
        interactions: List[dict], 
        all_products: List[dict],
        exclude_ids: set,
        n: int = 10
    ) -> List[Tuple[str, float]]:
        """
        Get recommendations based on product content similarity.
        """
        if not interactions:
            return []
        
        # Get similar products for recently interacted items
        similar_products = defaultdict(float)
        
        # Weight recent interactions more heavily
        for i, interaction in enumerate(reversed(interactions[:20])):
            product_id = interaction.get('product_id')
            if not product_id:
                continue
            
            recency_weight = 1.0 - (i * 0.03)  # Decreasing weight for older interactions
            similar = self.get_similar_products(product_id, n=5)
            
            for sim_id in similar:
                if sim_id not in exclude_ids:
                    similar_products[sim_id] += recency_weight
        
        # Sort by score
        sorted_products = sorted(similar_products.items(), key=lambda x: x[1], reverse=True)
        return sorted_products[:n]
    
    def get_collaborative_recommendations(
        self,
        user_interactions: List[dict],
        all_user_interactions: List[dict],
        exclude_ids: set,
        n: int = 10
    ) -> List[Tuple[str, float]]:
        """
        Simple collaborative filtering: "Users who bought X also bought Y"
        """
        if not user_interactions:
            return []
        
        # Get products the user has interacted with
        user_products = set(i.get('product_id') for i in user_interactions if i.get('product_id'))
        
        # Find users who interacted with the same products
        user_purchase_map = defaultdict(set)
        for interaction in all_user_interactions:
            user_id = interaction.get('user_id')
            product_id = interaction.get('product_id')
            if user_id and product_id:
                user_purchase_map[user_id].add(product_id)
        
        # Find similar users (Jaccard similarity)
        similar_user_products = defaultdict(float)
        for other_user_id, other_products in user_purchase_map.items():
            if other_products == user_products:
                continue
            
            # Calculate Jaccard similarity
            intersection = len(user_products & other_products)
            if intersection > 0:
                union = len(user_products | other_products)
                similarity = intersection / union
                
                # Add their products weighted by similarity
                for product_id in other_products - user_products - exclude_ids:
                    similar_user_products[product_id] += similarity
        
        sorted_products = sorted(similar_user_products.items(), key=lambda x: x[1], reverse=True)
        return sorted_products[:n]
    
    def get_popularity_recommendations(
        self,
        interaction_counts: Dict[str, int],
        purchase_counts: Dict[str, int],
        exclude_ids: set,
        n: int = 10
    ) -> List[Tuple[str, float]]:
        """
        Get trending/popular products for cold start.
        Combines view counts and purchase counts.
        """
        product_scores = defaultdict(float)
        
        for product_id, count in interaction_counts.items():
            if product_id not in exclude_ids:
                product_scores[product_id] += count * 0.3  # View weight
        
        for product_id, count in purchase_counts.items():
            if product_id not in exclude_ids:
                product_scores[product_id] += count * 2.0  # Purchase weight
        
        sorted_products = sorted(product_scores.items(), key=lambda x: x[1], reverse=True)
        return sorted_products[:n]
    
    def get_recommendations(
        self,
        user_id: str,
        user_interactions: List[dict],
        all_user_interactions: List[dict],
        all_products: List[dict],
        interaction_counts: Dict[str, int],
        purchase_counts: Dict[str, int],
        n: int = 10
    ) -> List[dict]:
        """
        Main recommendation method combining all strategies.
        
        For users with history:
        - 40% content-based (similar to what they viewed/bought)
        - 30% collaborative (what similar users bought)
        - 30% preference-based (based on category/material preferences)
        
        For cold start (no history):
        - 100% popularity-based (trending products)
        """
        
        # Ensure products are fitted
        if not self.is_fitted:
            self.fit_products(all_products)
        
        # Get products user has already interacted with
        exclude_ids = set(i.get('product_id') for i in user_interactions if i.get('product_id'))
        
        recommended_ids = {}
        
        if user_interactions:
            # User has history - use hybrid approach
            logger.info(f"Generating personalized recommendations for user {user_id}")
            
            # Content-based recommendations
            content_recs = self.get_content_based_recommendations(
                user_interactions, all_products, exclude_ids, n=n
            )
            for product_id, score in content_recs:
                recommended_ids[product_id] = recommended_ids.get(product_id, 0) + score * 0.4
            
            # Collaborative recommendations
            collab_recs = self.get_collaborative_recommendations(
                user_interactions, all_user_interactions, exclude_ids, n=n
            )
            for product_id, score in collab_recs:
                recommended_ids[product_id] = recommended_ids.get(product_id, 0) + score * 0.3
            
            # Preference-based scoring
            preferences = self.calculate_user_preferences(user_interactions)
            for product in all_products:
                product_id = product.get('id')
                if product_id and product_id not in exclude_ids:
                    pref_score = self.score_product_for_user(product, preferences)
                    if pref_score > 0:
                        recommended_ids[product_id] = recommended_ids.get(product_id, 0) + pref_score * 0.3
        else:
            # Cold start - use popularity
            logger.info(f"Cold start: Using popularity recommendations for user {user_id}")
            popularity_recs = self.get_popularity_recommendations(
                interaction_counts, purchase_counts, exclude_ids, n=n
            )
            for product_id, score in popularity_recs:
                recommended_ids[product_id] = score
        
        # Sort by final score
        sorted_recommendations = sorted(recommended_ids.items(), key=lambda x: x[1], reverse=True)[:n]
        
        # Build result with full product data
        results = []
        for product_id, score in sorted_recommendations:
            product = self.product_data.get(product_id)
            if product:
                result = dict(product)
                result['recommendation_score'] = round(score, 3)
                results.append(result)
        
        # If we don't have enough, fill with random products (excluding only already-recommended ones)
        if len(results) < n:
            remaining = n - len(results)
            seen_ids = set(r['id'] for r in results)
            for product in all_products:
                if product.get('id') not in seen_ids:
                    result = dict(product)
                    result['recommendation_score'] = 0.1
                    results.append(result)
                    if len(results) >= n:
                        break
        
        # Ultimate fallback: if still empty (shouldn't happen), return popular products
        if len(results) == 0 and all_products:
            popularity_recs = self.get_popularity_recommendations(
                interaction_counts, purchase_counts, set(), n=n
            )
            for product_id, score in popularity_recs:
                product = self.product_data.get(product_id)
                if product:
                    result = dict(product)
                    result['recommendation_score'] = round(score, 3)
                    result['fallback'] = 'trending'
                    results.append(result)
        
        return results


# Global instance
recommendation_engine = RecommendationEngine()
