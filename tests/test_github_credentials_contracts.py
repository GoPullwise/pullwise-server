from copy import deepcopy
import pytest

from pullwise_server.github_credentials import GitHubCredentialResolver
from pullwise_server.github_transport import GitHubUnavailable


@pytest.fixture
def setup():
    target = dict(control_key='control', module='pr', resource_kind='repository',
                  resource_id='github:10', repository_id='github:10', github_repository_id='10',
                  billing_owner_id='owner', installation_id='20', app_id='30',
                  enabled=True, accessible=1, valid_until=300, authorization_revision=1)
    account = dict(id='owner', githubId='40')
    identity = dict(id='identity', userId='owner', githubUserId='40', accessToken='user-secret', status='active')
    access = dict(githubIdentityId='identity', githubAppInstallationId='20', canAccess=True)
    state = dict(target=target, account=account, identity=identity, access=access, now=100, calls=[])
    def install(installation_id):
        state['calls'].append(installation_id)
        return dict(token='install-secret', expires_at='2099-01-01T00:00:00Z')
    resolver = GitHubCredentialResolver(
        current_target=lambda key: deepcopy(state['target']),
        current_account=lambda owner: deepcopy(state['account']),
        identities_for_user=lambda user: [deepcopy(state['identity'])],
        installation_access_for_user=lambda user, installation: deepcopy(state['access']),
        repository_for_account=lambda user, repo: dict(id=repo, fullName='org/repo'),
        app_id='30', app_token=lambda: 'app-secret', installation_token=install,
        clock=lambda: state['now'])
    return resolver, state, deepcopy(target)


def test_binding_and_reader_use_account_identity_and_installation(setup):
    resolver, state, target = setup
    binding = resolver.resolve_binding(target)
    assert binding.github_user_id == '40'
    assert binding.repository_id == '10'
    assert all(secret not in repr(binding) for secret in ('user-secret','app-secret','install-secret'))
    assert resolver.token_for_target(target) == 'install-secret'


@pytest.mark.parametrize('field,value', [('billing_owner_id','other'), ('installation_id','999'),
                                      ('authorization_revision',2), ('enabled',False)])
def test_rejects_stale_or_disabled_target_before_minting(setup, field, value):
    resolver, state, target = setup
    state['target'][field] = value
    with pytest.raises(GitHubUnavailable):
        resolver.resolve_binding(target)
    assert state['calls'] == []


@pytest.mark.parametrize('field,value', [('userId','other'),('status','revoked'),('githubUserId',True),('accessToken','')])
def test_rejects_invalid_identity(setup, field, value):
    resolver, state, target = setup
    state['identity'][field] = value
    with pytest.raises(GitHubUnavailable):
        resolver.resolve_binding(target)


def test_known_installation_denial_blocks_credentials(setup):
    resolver, state, target = setup
    state['access']['canAccess'] = False
    with pytest.raises(GitHubUnavailable):
        resolver.resolve_binding(target)
    assert state['calls'] == []


def test_expired_proof_blocks_reader_but_allows_renewal_binding(setup):
    resolver, state, target = setup
    state['now'] = 300
    with pytest.raises(GitHubUnavailable):
        resolver.token_for_target(target)
    assert state['calls'] == []
    assert resolver.resolve_binding(target).user_token == 'user-secret'


def test_personal_watch_needs_no_app_and_uses_owners_current_identity(setup):
    resolver, state, target = setup
    target.update(module='updates', resource_kind='watch', owner_id='owner', target_repository_id=None,
                  installation_id=None, app_id=None)
    state['target'] = deepcopy(target)
    assert resolver.token_for_target(target) == 'user-secret'
    assert state['calls'] == []


def test_shared_watch_binds_target_installation_not_upstream(setup):
    resolver, state, target = setup
    target.update(module='updates', resource_kind='watch', owner_id='owner', target_repository_id='github:99',
                  target_installation_id='20', target_billing_owner_id='owner', installation_id=None)
    state['target'] = deepcopy(target)
    binding = resolver.resolve_binding(target)
    assert binding.target_repository_id == '99'
    assert binding.installation_id == '20'
    assert resolver.token_for_target(target) == 'user-secret'


def test_late_token_response_cannot_cross_configuration_change(setup):
    resolver, state, target = setup
    def late(installation):
        state['target']['authorization_revision'] = 2
        return dict(token='install-secret', expires_at='2099-01-01T00:00:00Z')
    resolver.installation_token = late
    with pytest.raises(GitHubUnavailable):
        resolver.resolve_binding(target)


@pytest.mark.parametrize('payload', [dict(token='install-secret', expires_at='1970-01-01T00:00:01Z'),
                                     dict(token='install-secret'), dict(token='install-secret', expires_at='bad'),
                                     dict(token='install-secret', expires_at=float('inf'))])
def test_rejects_expired_or_unknown_installation_token_lifetime(setup, payload):
    resolver, state, target = setup
    resolver.installation_token = lambda installation: payload
    with pytest.raises(GitHubUnavailable):
        resolver.resolve_binding(target)


def test_sanitizes_adapter_failure_and_preserves_retry_deadline(setup):
    resolver, state, target = setup
    def fail(installation):
        raise GitHubUnavailable('install-secret', retry_at=999)
    resolver.installation_token = fail
    with pytest.raises(GitHubUnavailable) as error:
        resolver.resolve_binding(target)
    assert 'secret' not in str(error.value)
    assert error.value.retry_at == 999


def test_deleted_account_is_conclusive_absence_but_never_a_read_token(setup):
    resolver, state, target = setup
    state['account'] = None
    assert resolver.resolve_binding(target) is None
    with pytest.raises(GitHubUnavailable):
        resolver.token_for_target(target)


def test_account_unlink_during_mint_rejects_late_token(setup):
    resolver, state, target = setup
    def late(installation):
        state['account'] = None
        return dict(token='install-secret', expires_at='2099-01-01T00:00:00Z')
    resolver.installation_token = late
    with pytest.raises(GitHubUnavailable):
        resolver.resolve_binding(target)


def test_known_oauth_expiry_crossed_during_mint_rejects_binding(setup):
    resolver, state, target = setup
    state['identity']['expires_at'] = 101
    def late(installation):
        state['now'] = 101
        return dict(token='install-secret', expires_at='2099-01-01T00:00:00Z')
    resolver.installation_token = late
    with pytest.raises(GitHubUnavailable):
        resolver.resolve_binding(target)


def test_reader_does_not_mint_app_jwt_or_renew_proof(setup):
    resolver, state, target = setup
    def forbidden():
        raise AssertionError('App JWT must not be requested')
    resolver.app_token = forbidden
    assert resolver.token_for_target(target) == 'install-secret'
    assert state['target'] == target


@pytest.mark.parametrize('mutation', ['identity', 'installation_access'])
def test_local_credential_revocation_during_mint_rejects_binding(setup, mutation):
    resolver, state, target = setup
    def late(installation):
        if mutation == 'identity':
            state['identity']['status'] = 'disabled'
        else:
            state['access']['canAccess'] = False
        return dict(token='install-secret', expires_at='2099-01-01T00:00:00Z')
    resolver.installation_token = late
    with pytest.raises(GitHubUnavailable):
        resolver.resolve_binding(target)


def test_unrelated_identity_change_does_not_invalidate_selected_credential(setup):
    resolver, state, target = setup
    unrelated = dict(id='other', userId='owner', githubUserId='50', accessToken='other-secret', status='active')
    resolver.identities_for_user = lambda user: [deepcopy(state['identity']), deepcopy(unrelated)]
    def late(installation):
        unrelated['status'] = 'disabled'
        return dict(token='install-secret', expires_at='2099-01-01T00:00:00Z')
    resolver.installation_token = late
    assert resolver.resolve_binding(target).github_user_id == '40'
