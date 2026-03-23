import jwt
import time
import requests

class GitHubAppAuth:
    def __init__(self, app_id: str, private_key_path: str, installation_id: int):
        self.app_id = app_id
        self.installation_id = installation_id
        with open(private_key_path) as f:
            self.private_key = f.read()
        self._cached_token = None
        self._cached_at = 0

    def generate_jwt(self) -> str:
        """Generate a JWT signed with the app's private key."""
        payload = {
            'iat': int(time.time()),
            'exp': int(time.time()) + 600,  # Max 10 minutes
            'iss': self.app_id
        }
        return jwt.encode(payload, self.private_key, algorithm='RS256')

    def get_installation_token(self) -> str:
        """Get an installation access token (valid 60 minutes)."""
        # Refresh if token is older than 55 minutes (5 min buffer)
        if self._cached_token and time.time() - self._cached_at < 3300:
            return self._cached_token

        jwt_token = self.generate_jwt()
        response = requests.post(
            f'https://api.github.com/app/installations/{self.installation_id}/access_tokens',
            headers={
                'Authorization': f'Bearer {jwt_token}',
                'Accept': 'application/vnd.github+json'
            }
        )
        response.raise_for_status()
        self._cached_token = response.json()['token']
        self._cached_at = time.time()
        return self._cached_token
