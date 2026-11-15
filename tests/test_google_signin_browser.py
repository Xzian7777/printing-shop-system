import json
from pathlib import Path
import shutil
import subprocess
import unittest


class GoogleBrowserTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for browser flow checks')
    def test_same_domain_auth_for_both_production_addresses(self):
        template = (Path(__file__).parents[1] / 'templates/login.html').read_text(encoding='utf-8')
        config = 'const firebaseConfig = ' + template.split('const firebaseConfig = ', 1)[1].split('};', 1)[0] + '};'
        script = '''
const vm = require('node:vm');
const assert = require('node:assert/strict');
for (const hostname of ['kezcemzek.vercel.app','printing-shop-system.vercel.app','localhost','unrelated.vercel.app']) {
  const context = {window:{location:{hostname}}};
  const actual = vm.runInNewContext(SOURCE + '; firebaseConfig.authDomain',context);
  assert.equal(actual, ['kezcemzek.vercel.app','printing-shop-system.vercel.app'].includes(hostname)
    ? hostname : 'printingshop-7fd66.firebaseapp.com');
}
'''.replace('SOURCE', json.dumps(config))
        result = subprocess.run(['node'], input=script, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for browser flow checks')
    def test_popup_redirect_resume_cancellation_and_backend_failure(self):
        template = (Path(__file__).parents[1] / 'templates/login.html').read_text(encoding='utf-8')
        source = template.split('let googleSignInBusy = false;', 1)[1].split('</script>', 1)[0]
        source = 'let googleSignInBusy = false;' + source
        script = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
function setup(redirectResult = {}, pending = false, backendOk = true) {
  const button = {disabled:false};
  const terms = {checked:true};
  const alert = {classList:{remove(){}}, textContent:''};
  const values = new Map(pending ? [['kcz_google_redirect','1']] : []);
  const calls = {popup:0,redirect:0,fetch:0,signOut:0};
  const user = {getIdToken:async()=> 'synthetic-google-token'};
  const auth = {
    getRedirectResult:async()=>redirectResult,
    signInWithPopup:async()=>{calls.popup++;return {user};},
    signInWithRedirect:async()=>{calls.redirect++;},
    signOut:async()=>{calls.signOut++;}
  };
  function authFunction(){return auth;}
  authFunction.GoogleAuthProvider = function(){this.setCustomParameters=()=>{};};
  const env={firebase:{auth:authFunction},window:{location:{href:'/login'}},
    document:{getElementById:id=>id==='loginAlertBox'?alert:id==='loginTerms'?terms:button,querySelectorAll:()=>[button]},
    sessionStorage:{getItem:key=>values.get(key),setItem:(key,value)=>values.set(key,value),removeItem:key=>values.delete(key)},
    resetGoogleBtn:()=>{},
    fetch:async(path,options)=>{
      calls.fetch++;assert.equal(path,'/api/google-login');
      assert.deepEqual(JSON.parse(options.body),{idToken:'synthetic-google-token'});
      return {ok:backendOk,json:async()=>backendOk ? {success:true,redirect:'/admin'} : {success:false,message:'Account needs review'}};
    }};
  vm.runInNewContext(SOURCE,env);
  return {env,auth,calls,button,alert,values,user,terms};
}
const tick=()=>new Promise(setImmediate);
(async()=>{
  const declined=setup();await tick();declined.terms.checked=false;
  await declined.env.handleGoogleSignIn();
  assert.equal(declined.calls.popup,0);assert.equal(declined.calls.fetch,0);
  assert.equal(declined.terms.checked,false);assert.match(declined.alert.textContent,/accept the Terms/);
  const success=setup();await tick();await success.env.handleGoogleSignIn();
  assert.equal(success.calls.popup,1);assert.equal(success.env.window.location.href,'/admin');
  const blocked=setup();await tick();
  blocked.auth.signInWithPopup=async()=>{blocked.calls.popup++;throw {code:'auth/popup-blocked'};};
  await blocked.env.handleGoogleSignIn();await blocked.env.handleGoogleSignIn();
  assert.equal(blocked.calls.redirect,1);assert.equal(blocked.calls.popup,1);
  assert.equal(blocked.calls.fetch,0);assert.equal(blocked.values.get('kcz_google_redirect'),'1');
  const resumed=setup({user:{getIdToken:async()=> 'synthetic-google-token'}},true);await tick();
  assert.equal(resumed.calls.fetch,1);assert.equal(resumed.env.window.location.href,'/admin');
  assert.equal(resumed.values.has('kcz_google_redirect'),false);
  const cancelled=setup();await tick();
  cancelled.auth.signInWithPopup=async()=>{throw {code:'auth/popup-closed-by-user'};};
  await cancelled.env.handleGoogleSignIn();
  assert.equal(cancelled.button.disabled,false);assert.equal(cancelled.calls.fetch,0);
  const rejected=setup({},false,false);await tick();await rejected.env.handleGoogleSignIn();
  assert.equal(rejected.env.window.location.href,'/login');assert.equal(rejected.button.disabled,false);
  assert.equal(rejected.alert.textContent,'Account needs review');
})().catch(error=>{console.error(error);process.exitCode=1});
'''.replace('SOURCE', json.dumps(source))
        result = subprocess.run(['node'], input=script, text=True, capture_output=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
