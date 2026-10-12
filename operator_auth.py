"""Station-to-ledger login proxy. Credentials and sessions are never saved locally."""
import ipaddress
import json
import threading
import urllib.parse
import urllib.request

LOCK=threading.Lock()
SESSION=None


def base_url(config):
    parsed=urllib.parse.urlsplit(config.get('mint_endpoint',''))
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Configure the ledger server mint endpoint first')
    if parsed.scheme=='http':
        try:
            address=ipaddress.ip_address(parsed.hostname)
            protected=address.is_loopback or address in ipaddress.ip_network('100.64.0.0/10') or address in ipaddress.ip_network('fd7a:115c:a1e0::/48')
        except ValueError:
            protected=parsed.hostname=='localhost'
        if not protected:
            raise ValueError('Use HTTPS or a loopback/Tailscale address for password authentication')
    return urllib.parse.urlunsplit((parsed.scheme,parsed.netloc,parsed.path.rsplit('/',1)[0],'',''))


def request(config,path,payload=None,session=None):
    body=json.dumps(payload).encode() if payload is not None else None
    headers={'Content-Type':'application/json','Authorization':'Bearer '+config.get('ledger_auth_token','')}
    if session:headers['X-PiForge-Session']=session
    req=urllib.request.Request(base_url(config)+path,data=body,headers=headers)
    try:
        with urllib.request.urlopen(req,timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:detail=json.loads(error.read()).get('error','Authentication failed')
        except (ValueError,AttributeError):detail='Server rejected authentication'
        raise ValueError(detail) from None


def login(config,username,password,register=False):
    global SESSION
    if register:request(config,'/auth/register',{'username':username,'password':password})
    result=request(config,'/auth/login',{'username':username,'password':password})
    with LOCK:SESSION={'base':base_url(config),'token':result['token'],'user':result['user']}
    return result['user']


def current(config,verify=True):
    with LOCK:snapshot=dict(SESSION) if SESSION else None
    if not snapshot or snapshot['base']!=base_url(config):return None
    if verify:
        result=request(config,'/auth/me',session=snapshot['token'])
        return result.get('user')
    return snapshot['user']


def token(config):
    with LOCK:snapshot=dict(SESSION) if SESSION else None
    if not snapshot or snapshot['base']!=base_url(config):
        raise ValueError('Sign in to the ledger server before installing a card')
    return snapshot['token']


def logout(config):
    global SESSION
    with LOCK:snapshot=SESSION;SESSION=None
    if snapshot:request(config,'/auth/logout',{},snapshot['token'])
