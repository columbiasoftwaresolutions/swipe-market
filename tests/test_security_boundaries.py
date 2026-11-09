import os
import unittest
from unittest.mock import patch


# These tests use an isolated in-memory database, even if a production URL is set.
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
os.environ['SECRET_KEY'] = 'test-only-secret-key'
os.environ['GOOGLE_CLIENT_ID'] = 'test-client-id'
os.environ['GOOGLE_CLIENT_SECRET'] = 'test-client-secret'
os.environ['GMAIL_REFRESH_TOKEN'] = 'test-refresh-token'

from app import app, db, User, SellerListing, BuyerListing  # noqa: E402


class SecurityBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(app.config['SQLALCHEMY_DATABASE_URI'], 'sqlite:///:memory:')
        app.config['TESTING'] = True
        self.context = app.app_context()
        self.context.push()
        db.create_all()

        alice = User(name='Alice', email='alice@columbia.edu', phone='111-1111')
        bob = User(name='Bob', email='bob@barnard.edu', phone='222-2222')
        db.session.add_all([alice, bob])
        db.session.flush()
        self.alice_id = alice.id
        self.bob_id = bob.id
        bob_listing = BuyerListing(
            dining_hall='John Jay', date='2099-01-01', start_time='12:00',
            end_time='13:00', price=5, payment_methods='Cash', user_id=bob.id,
        )
        alice_listing = SellerListing(
            dining_hall='John Jay', date='2099-01-01', start_time='12:00',
            end_time='13:00', price=5, payment_methods='Cash', user_id=alice.id,
        )
        db.session.add_all([bob_listing, alice_listing])
        db.session.commit()
        self.bob_listing_id = bob_listing.id
        self.client = app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def request(self, method, path, **kwargs):
        return getattr(self.client, method)(path, base_url='https://localhost', **kwargs)

    def sign_in(self, email='alice@columbia.edu'):
        with patch('app.id_token.verify_oauth2_token', return_value={
            'email': email, 'sub': 'test-google-user-id',
        }):
            response = self.request('post', '/api/auth/google', json={'id_token': 'test-token'})
        self.assertEqual(response.status_code, 200)

    def test_email_query_parameter_cannot_sign_in(self):
        response = self.request('get', '/', query_string={'email': 'bob@barnard.edu'})
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as current_session:
            self.assertNotIn('user_email', current_session)
        self.assertEqual(self.request('get', '/api/get_profile').status_code, 302)

    def test_old_unverified_session_cannot_access_user_data(self):
        with self.client.session_transaction() as current_session:
            current_session['user_id'] = 'old-google-id'
            current_session['user_email'] = 'bob@barnard.edu'
        self.assertFalse(self.request('get', '/api/auth/status').json['authenticated'])
        self.assertEqual(self.request('get', '/api/get_profile').status_code, 302)
        html = self.request('get', '/').get_data(as_text=True)
        self.assertNotIn('data-is-owner="true"', html)

    def test_session_with_only_user_id_cannot_access_user_data(self):
        with self.client.session_transaction() as current_session:
            current_session['user_id'] = 'old-google-id'
        self.assertFalse(self.request('get', '/api/auth/status').json['authenticated'])
        self.assertEqual(self.request('get', '/api/get_profile').status_code, 302)

    def test_email_query_parameter_cannot_switch_authenticated_user(self):
        self.sign_in()
        html = self.request('get', '/', query_string={'email': 'bob@barnard.edu'}).get_data(as_text=True)
        self.assertEqual(html.count('data-is-owner="true"'), 1)
        self.assertEqual(self.request('get', '/api/get_profile').json['email'], 'alice@columbia.edu')

    def test_listing_html_does_not_expose_owner_emails(self):
        self.sign_in()
        html = self.request('get', '/').get_data(as_text=True)
        self.assertNotIn('alice@columbia.edu', html)
        self.assertNotIn('bob@barnard.edu', html)
        self.assertEqual(html.count('data-is-owner="true"'), 1)
        self.assertEqual(html.count('data-is-owner="false"'), 1)

    def test_check_user_only_returns_signed_in_users_details(self):
        self.sign_in()
        other = self.request('post', '/api/check_user', json={'email': 'bob@barnard.edu'})
        self.assertEqual(other.status_code, 403)
        self.assertNotIn('222-2222', other.get_data(as_text=True))

        own = self.request('post', '/api/check_user', json={'email': 'alice@columbia.edu'})
        self.assertEqual(own.status_code, 200)
        self.assertEqual(own.json['phone'], '111-1111')

    def test_posted_listing_uses_session_owner(self):
        self.sign_in()
        response = self.request('post', '/submit_listing', data={
            'poster_email': 'bob@barnard.edu',
            'listing_type': 'seller',
            'dining_hall[]': 'John Jay',
            'date': '2099-01-01',
            'start_time': '12:00',
            'end_time': '13:00',
            'price': '5',
            'payment_methods[]': 'Cash',
        })
        self.assertEqual(response.status_code, 302)
        listings = SellerListing.query.order_by(SellerListing.id).all()
        self.assertEqual(len(listings), 2)
        self.assertEqual(listings[-1].user_id, self.alice_id)

    def test_unauthorized_delete_does_not_reveal_owner_email(self):
        self.sign_in()
        response = self.request('post', f'/delete_listing/{self.bob_listing_id}',
                                data={'listing_type': 'buyer'})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn('bob@barnard.edu', response.get_data(as_text=True))
        self.assertTrue(db.session.get(BuyerListing, self.bob_listing_id).is_active)

    def test_logout_clears_server_session(self):
        self.sign_in()
        self.assertTrue(self.request('get', '/api/auth/status').json['authenticated'])
        self.assertEqual(self.request('get', '/api/get_profile').status_code, 200)
        self.assertEqual(self.request('post', '/api/auth/logout').status_code, 200)
        self.assertFalse(self.request('get', '/api/auth/status').json['authenticated'])
        self.assertEqual(self.request('get', '/api/get_profile').status_code, 302)


if __name__ == '__main__':
    unittest.main()
