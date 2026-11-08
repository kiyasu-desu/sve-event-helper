// Offline UI contract tests: fake DOM and fetch, no browser/network/production state.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function harness(pathname='/') {
  const elements = new Map();
  const events = {};
  function element() {
    let html='';
    const queried=new Map();
    return {children:[],value:'',textContent:'',style:{},dataset:{},checked:false,
      get innerHTML(){return html;},set innerHTML(value){html=value;this.children=[];},
      classList:{add(){},remove(){},toggle(){},contains(){return false;}},
      events:{},addEventListener(name,fn){this.events[name]=fn;},querySelectorAll(){return [];},
      querySelector(selector){if(!queried.has(selector))queried.set(selector,element());return queried.get(selector);},
      appendChild(child){this.children.push(child);},removeChild(child){this.children.splice(this.children.indexOf(child),1);},
      focus(){},scrollIntoView(){},replaceWith(){},removeAttribute(){},setAttribute(){},getAttribute(){return null;},
      firstElementChild:{},hidden:false};
  }
  const context = vm.createContext({console, URL, Map, Set, Date, Promise,
    crypto:require('node:crypto').webcrypto, encodeURIComponent, AbortController,
    setTimeout(){return 1;},clearTimeout(){},setInterval(){return 1;},
    confirm(){return true;},alert(){},navigator:{},addEventListener(){},location:{pathname},history:{},
    window:{scrollY:0,scrollTo(){},addEventListener(name,fn){events[name]=fn;},location:{},innerHeight:800},
    document:{body:{style:{},children:[]},createTextNode:text=>text,getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id);},
      querySelectorAll(){return [];},addEventListener(name,fn){events[name]=fn;},createElement:element},
    fetch(){throw new Error('fetch must be stubbed');}});
  const historyEntries=[null];
  let historyIndex=0;
  Object.assign(context.history, {
    state:null,
    pushState(state){
      historyEntries.splice(++historyIndex);
      historyEntries.push(state);
      this.state=state;
    },
    back(){
      if(historyIndex===0) return;
      this.state=historyEntries[--historyIndex];
      events.popstate();
    }
  });
  let source = fs.readFileSync('app/client/ui/index.html','utf8').match(/<script\b[^>]*>([\s\S]*?)<\/script>/)[1];
  source = source.slice(0, source.lastIndexOf('setInterval(refresh'));
  vm.runInContext(source, context);
  return {context,elements,events,run:code=>vm.runInContext(code,context)};
}

function state(nickname='test-shared') {
  return {ok:true,active_account_id:'10001',accounts:[
    {account_id:'10001',uid:10001,name:nickname,nickname,version:1,active:true},
    {account_id:'10002',uid:10002,name:nickname,nickname,version:1,active:false}],
    login:{},remote:{enabled:false},mine:[],can_login:false};
}

test('returning to events reloads cards and keeps the selected area and valid picks',async()=>{
  const h=harness();
  const open={tournament_id:'test-open',name:'test-open',group:'open',status:'可报名',
    is_open:true,player_num:2,max_player_number:16};
  const enrolling={...open,tournament_id:'test-enrolling',name:'test-enrolling'};
  const removed={...open,tournament_id:'test-removed',name:'test-removed'};
  const calls=[];
  const oldButtons=[{disabled:false},{disabled:false}];
  h.context.document.querySelectorAll=selector=>
    selector==='#tourList button.act, #myList button.act' ? oldButtons : [];
  let releaseTournaments;
  h.context.fetch=async path=>{
    const url=new URL(path,'https://example.com');
    calls.push(url);
    if(url.pathname==='/api/state') return {json:async()=>state()};
    assert.equal(url.pathname,'/api/tournaments');
    return new Promise(resolve=>{releaseTournaments=()=>resolve({
      json:async()=>({ok:true,account_id:'10001',items:[
        {...open,player_num:3},{...enrolling,group:'mine',status:'已报名'}]})
    });});
  };
  h.run(`currentPage="subs"; notifyStarted=true; AREAS=[{name:"测试省",cities:["测试市"]}];
    $("province").value="测试省"; $("city").value="测试市";
    PICK_ACCTS.add("10001"); PICK_TOURS.add("test-open"); PICK_TOURS.add("test-removed");`);
  h.context.TOURS_BEFORE_RETURN=[{...open,is_open:false},enrolling,removed];
  h.run('TOURS=TOURS_BEFORE_RETURN; renderSections();');
  assert.match(h.elements.get('tourList').innerHTML,/test-enrolling/);
  assert.match(h.elements.get('tourList').innerHTML,/<b>2<\/b>\/16/);
  assert.match(h.elements.get('tourList').innerHTML,/subscribe\('test-open'\)/);
  assert.equal(h.elements.get('btnBatch').hidden,true);

  h.context.location.pathname='/';
  h.context.window.scrollTo=()=>{};
  h.events.popstate();
  assert.equal(h.elements.get('btnBatch').disabled,true);
  assert.ok(oldButtons.every(button=>button.disabled));
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(h.elements.get('btnBatch').disabled,true);
  await h.run('Promise.all([subscribe("test-open"),unsub("test-open"),enroll("test-open"),cancelEnroll("test-open")])');
  assert.equal(h.run('PICK_TOURS.size'),2);
  await h.run('batchEnroll()');
  releaseTournaments();
  await new Promise(resolve=>setImmediate(resolve));

  assert.deepEqual(calls.map(url=>url.pathname),['/api/state','/api/tournaments']);
  assert.equal(calls[1].searchParams.get('area'),'测试省-测试市');
  assert.equal(calls[1].searchParams.get('account_id'),'10001');
  assert.match(h.elements.get('tourList').innerHTML,/<b>3<\/b>\/16/);
  assert.match(h.elements.get('tourList').innerHTML,/立即报名/);
  assert.match(h.elements.get('tourList').innerHTML,/data-id="test-open" checked/);
  assert.doesNotMatch(h.elements.get('tourList').innerHTML,/test-enrolling|test-removed/);
  assert.match(h.elements.get('myList').innerHTML,/test-enrolling[\s\S]*已报名/);
  assert.equal(h.run('PICK_ACCTS.has("10001")'),true);
  assert.equal(h.run('PICK_TOURS.has("test-removed")'),false);
  assert.equal(h.elements.get('btnBatch').disabled,false);
});

test('an obsolete events visit cannot start a tournament query after a newer visit',async()=>{
  const h=harness();
  h.run(`currentPage="subs"; notifyStarted=true; ACCOUNT_ID="10001";
    AREAS=[{name:"测试省",cities:["测试市"]}];
    $("province").value="测试省"; $("city").value="测试市";`);
  const statePolls=[], tournamentPolls=[];
  h.context.fetch=path=>new Promise(resolve=>{
    const polls=path.startsWith('/api/state') ? statePolls : tournamentPolls;
    polls.push(data=>resolve({json:async()=>data}));
  });
  h.context.window.scrollTo=()=>{};
  for (const pathname of ['/','/subs','/']) {
    h.context.location.pathname=pathname;
    h.events.popstate();
  }
  statePolls[2](state());
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(tournamentPolls.length,1);
  statePolls[0](state());
  statePolls[1](state());
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(tournamentPolls.length,1);
  tournamentPolls[0]({ok:true,account_id:'10001',
    items:[{tournament_id:'test-event',name:'test-event',group:'open'}]});
  await new Promise(resolve=>setImmediate(resolve));
  assert.match(h.elements.get('tourList').innerHTML,/立即报名/);
});

test('a successful tournament query waits for state recovery before enabling actions',async()=>{
  const h=harness();
  h.run(`currentPage="subs"; notifyStarted=true; ACCOUNT_ID="10001"; MINE.add("test-event");
    AREAS=[{name:"测试省",cities:["测试市"]}];
    $("province").value="测试省"; $("city").value="测试市";`);
  let stateFailed=true;
  const posts=[];
  h.context.fetch=async(path,opts)=>{
    if(opts.method==='POST') posts.push(path);
    if(path.startsWith('/api/state')) {
      if(stateFailed) throw Error('offline');
      return {json:async()=>state()};
    }
    return {json:async()=>({ok:true,account_id:'10001',
      items:[{tournament_id:'test-event',name:'test-event',group:'mine',status:'已报名'}]})};
  };
  h.context.window.scrollTo=()=>{};
  h.events.popstate();
  await new Promise(resolve=>setImmediate(resolve));
  const list=h.elements.get('myList');
  assert.match(list.innerHTML,/test-event[\s\S]*disabled>待核对/);
  await h.run('unsub("test-event")');
  assert.deepEqual(posts,[]);
  stateFailed=false;
  await h.run('refresh()');
  assert.doesNotMatch(list.innerHTML,/取消订阅|待核对/);
  assert.match(list.innerHTML,/取消报名/);
});

test('returning to subscriptions keeps an expanded log position when another card was added',async()=>{
  const h=harness('/subs');
  const lines=Array.from({length:60},(_,index)=>`test-log-${index}`);
  let subs=[{key:'10001|test-retained',logs:lines}];
  h.context.fetch=async()=>({json:async()=>({...state(),sub_count:subs.length,
    subs_grouped:{'10001':subs}})});
  h.context.window.scrollTo=()=>{};
  const show=async pathname=>{
    h.context.location.pathname=pathname;
    h.events.popstate();
    await new Promise(resolve=>setImmediate(resolve));
  };
  await show('/subs');
  const groups=h.elements.get('groups');
  const card=groups.children[0].children[1].children[0];
  const log=card.querySelector('[data-role="log"]');
  card.querySelector('[data-role="head"]').onclick();
  log.scrollHeight=900;
  log.clientHeight=220;
  log.scrollTop=175;
  log.onscroll();
  // Simulate a browser resetting the offset when the old group is detached.
  const groupHtml=Object.getOwnPropertyDescriptor(groups,'innerHTML');
  Object.defineProperty(groups,'innerHTML',{...groupHtml,set(value){
    groupHtml.set.call(this,value);
    log.scrollTop=0;
  }});

  await show('/');
  subs=[...subs,{key:'10001|test-added'}];
  await h.run('refresh()');
  await show('/subs');

  const cards=groups.children[0].children[1].children;
  assert.equal(cards.length,2);
  assert.equal(cards[0],card);
  assert.equal(card.querySelector('[data-role="body"]').style.display,'');
  assert.equal(log.scrollTop,175);
});

test('returning to subscriptions keeps the visible log line after a wrapped line rotates out',async()=>{
  const h=harness();
  const original=Array.from({length:60},(_,index)=>`test-log-${index}`);
  let logs=original;
  h.context.fetch=async()=>({json:async()=>({...state(),sub_count:1,
    subs_grouped:{'10001':[{key:'10001|test-event',logs}]}})});
  h.context.window.scrollTo=()=>{};
  h.context.location.pathname='/subs';
  h.events.popstate();
  await new Promise(resolve=>setImmediate(resolve));

  const groups=h.elements.get('groups');
  const card=groups.children[0].children[1].children[0];
  const log=card.querySelector('[data-role="log"]');
  const latest=card.querySelector('[data-role="latest"]');
  const body=card.querySelector('[data-role="body"]');
  card.querySelector('[data-role="head"]').onclick();
  assert.deepEqual(log.children.map(row=>row.textContent),original);
  const firstRetained=log.children[1];
  // The first record wraps on a narrow screen and takes two visual lines.
  log.children[0].offsetTop=6;
  log.children[1].offsetTop=36;
  log.scrollHeight=600;
  log.clientHeight=100;
  log.scrollTop=200;
  log.onscroll();

  h.context.location.pathname='/';
  h.run('notifyStarted=true;');
  h.events.popstate();
  await new Promise(resolve=>setImmediate(resolve));
  logs=[...original.slice(1),'test-log-60'];
  await h.run('refresh()');
  h.context.location.pathname='/subs';
  h.events.popstate();
  await new Promise(resolve=>setImmediate(resolve));

  assert.equal(groups.children[0].children[1].children[0],card);
  assert.equal(body.style.display,'');
  assert.equal(latest.textContent,'最新 test-log-60');
  assert.deepEqual(log.children.map(row=>row.textContent),logs);
  assert.equal(log.children[0],firstRetained);
  assert.equal(log.scrollTop,170);
});

for (const switchedAccount of [false,true]) {
  test(switchedAccount ? 'late cancellation leaves the new account subscription intact'
    : 'successful cancellation from subscriptions updates event cards even if refresh fails',async()=>{
    const h=harness('/subs');
    h.run(`currentPage="subs"; ACCOUNT_ID="10001";
      MINE.add("test-event");
      TOURS=[{tournament_id:"test-event",name:"test-event",group:"bookable"}];
      subscriptionView.update({ok:true,subs_grouped:{"10001":[{key:"10001|test-event"}]}});`);
    const card=h.elements.get('groups').children[0].children[1].children[0];
    let releaseCancellation;
    h.context.fetch=async path=>{
      if(path.startsWith('/api/unsubscribe')) {
        return new Promise(resolve=>{releaseCancellation=()=>resolve({json:async()=>({ok:true})});});
      }
      throw Error('offline');
    };
    const cancellation=card.querySelector('[data-role="unsub"]').onclick({stopPropagation(){}});
    // The user has returned to events while the cancellation is still in flight.
    h.run('currentPage="events"; $("subsPage").hidden=true; renderSections();');
    if(switchedAccount) h.run('ACCOUNT_ID="10002"; ACCOUNT_EPOCH++;');
    assert.match(h.elements.get('myList').innerHTML,/处理中/);
    releaseCancellation();
    await cancellation;
    assert.equal(h.run('MINE.has("test-event")'),switchedAccount);
    assert.equal(h.run('CONFIRMED.get("test-event")?.want'),switchedAccount ? undefined : false);
    if(switchedAccount) assert.match(h.elements.get('myList').innerHTML,/test-event[\s\S]*待核对/);
    else {
      assert.doesNotMatch(h.elements.get('myList').innerHTML,/test-event/);
      assert.match(h.elements.get('tourList').innerHTML,/test-event[\s\S]*待核对/);
    }
  });
}

test('confirmed cancellation removes the card before polling and ignores an older snapshot',async()=>{
  const h=harness('/subs');
  const snapshot={...state(),sub_count:1,subs_grouped:{'10001':[{key:'10001|test-event'}]}};
  h.context.snapshot=snapshot;
  h.run('ACCOUNT_ID="10001"; subscriptionView.update(snapshot);');
  const groups=h.elements.get('groups');
  const button=groups.children[0].children[1].children[0].querySelector('[data-role="unsub"]');
  const polls=[];
  let cancellationRequests=0;
  h.context.fetch=async path=>{
    if(path.startsWith('/api/unsubscribe')) {
      cancellationRequests++;
      return {json:async()=>({ok:true})};
    }
    return new Promise((resolve,reject)=>polls.push({resolve,reject}));
  };
  const oldPoll=h.run('refresh()');
  const cancel=button.onclick({stopPropagation(){}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(polls.length,2);
  assert.equal(groups.children.length,0);
  polls[0].resolve({json:async()=>snapshot});
  await oldPoll;
  assert.equal(groups.children.length,0);
  await button.onclick({stopPropagation(){}});
  assert.equal(cancellationRequests,1);
  polls[1].reject(Error('offline'));
  await cancel;
  assert.equal(groups.children.length,0);
  assert.match(h.elements.get('summary').textContent,/网络不可用/);
});

test('resubscribing while the subscriptions tab is hidden re-enables cancellation on the reused card',async()=>{
  const h=harness();
  const snapshot='{ok:true,subs_grouped:{"10001":[{key:"10001|test-event"}]}}';
  h.run(`ACCOUNT_ID="10001"; reloadAll=async()=>{}; subscriptionView.update(${snapshot});`);
  const groups=h.elements.get('groups');
  const card=groups.children[0].children[1].children[0];
  const button=card.querySelector('[data-role="unsub"]');
  const posts=[];
  let releaseCancellation;
  h.context.fetch=async(path,opts)=>{
    if(opts.method==='POST') posts.push(path.split('?')[0]);
    if(path.startsWith('/api/unsubscribe')) {
      return new Promise(resolve=>{releaseCancellation=()=>resolve({json:async()=>({ok:true})});});
    }
    return {json:async()=>state()};
  };
  const cancellation=button.onclick({stopPropagation(){}});
  assert.equal(button.disabled,true);
  h.run('$("subsPage").hidden=true;');
  await h.run('subscribe("test-event")');
  assert.deepEqual(posts,['/api/unsubscribe']);
  releaseCancellation();
  await cancellation;
  await h.run('subscribe("test-event")');
  assert.deepEqual(posts,['/api/unsubscribe','/api/subscribe']);
  h.run(`subscriptionView.update(${snapshot}); $("subsPage").hidden=false; subscriptionView.render();`);
  assert.equal(groups.children[0].children[1].children[0],card);
  assert.equal(button.disabled,false);
  assert.equal(button.textContent,'取消订阅');
});

test('an in-flight event subscription blocks cancellation only for the matching account and event',async()=>{
  const h=harness();
  h.run('ACCOUNT_ID="10001"; reloadAll=async()=>{};');
  const posts=[];
  let releaseSubscription;
  h.context.fetch=async(path,opts)=>{
    if(opts.method==='POST') posts.push(path.split('?')[0]);
    if(path.startsWith('/api/subscribe?')) {
      return new Promise(resolve=>{releaseSubscription=()=>resolve({json:async()=>({ok:true})});});
    }
    return {json:async()=>state()};
  };
  const subscription=h.run('subscribe("test-event")');
  h.run(`subscriptionView.update({ok:true,subs_grouped:{
    "10001":[{key:"10001|test-event"}],"10002":[{key:"10002|test-event"}]}});`);
  const groups=h.elements.get('groups');
  const buttons=groups.children.map(group=>
    group.children[1].children[0].querySelector('[data-role="unsub"]'));
  await buttons[0].onclick({stopPropagation(){}});
  assert.deepEqual(posts,['/api/subscribe']);
  assert.equal(buttons[0].disabled,true);
  assert.equal(buttons[1].disabled,false);
  releaseSubscription();
  await subscription;
  assert.equal(buttons[0].disabled,false);
  await buttons[0].onclick({stopPropagation(){}});
  assert.deepEqual(posts,['/api/subscribe','/api/unsubscribe']);
  assert.equal(h.run('MINE.has("test-event")'),false);
});

test('opening subscriptions offline renders the last successful hidden snapshot',async()=>{
  const h=harness();
  const groups=h.run('currentPage="events"; $("subsPage").hidden=true; $("groups");');
  h.context.fetch=async()=>({json:async()=>({...state(),sub_count:1,
    subs_grouped:{'10001':[{key:'10001|test-event',logs:['test-cached-log']}]}})});
  await h.run('refresh()');
  assert.equal(groups.children.length,0);
  h.context.fetch=async()=>{throw Error('offline');};
  await h.run('refresh()');

  h.context.location.pathname='/subs';
  h.context.window.scrollTo=()=>{};
  h.events.popstate();
  await new Promise(resolve=>setImmediate(resolve));
  assert.match(h.elements.get('summary').textContent,/网络不可用/);
  const card=groups.children[0]?.children[1].children[0];
  assert.ok(card);
  assert.equal(card.querySelector('[data-role="latest"]').textContent,'最新 test-cached-log');
});

test('browsers without modal dialogs can follow navigation links without a menu history entry',async()=>{
  const h=harness();
  const history=h.context.history;
  const entries=[];
  history.state={pageMenu:true};
  history.pushState=(state,_title,pathname)=>{
    entries.push({state,pathname});
    history.state=state;
    h.context.location.pathname=pathname;
  };
  h.context.window.scrollTo=()=>{};
  h.context.fetch=async()=>({json:async()=>state()});
  h.run('notifyStarted=true;');
  h.events.popstate();
  assert.equal(h.elements.get('navToggle').hidden,true);
  assert.equal(h.elements.get('navToggle').onclick,undefined);
  h.events.click({button:0,preventDefault(){},target:{
    closest:()=>({dataset:{page:'subs'},href:'/subs'})
  }});
  await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(entries,[{state:null,pathname:'/subs'}]);
  assert.equal(h.elements.get('subsPage').hidden,false);
});

test('a prefixed subscription deep link and its navigation stay under the mount',async()=>{
  const h=harness('/test-mount/subs');
  const calls=[];
  h.context.fetch=async path=>{
    calls.push(new URL(path,'https://example.com').pathname);
    return {json:async()=>state()};
  };
  h.context.window.scrollTo=()=>{};
  h.events.popstate();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(h.elements.get('eventsPage').hidden,true);
  assert.equal(h.elements.get('subsPage').hidden,false);
  assert.deepEqual(calls,['/test-mount/api/state']);

  const html=fs.readFileSync('app/client/ui/index.html','utf8');
  const links=[...html.matchAll(/href="([^"]+)" data-page="(events|subs)"/g)]
    .map(([,href,page])=>[new URL(href,'https://example.com/test-mount/subs').pathname,page]);
  assert.deepEqual(links,[['/test-mount/','events'],['/test-mount/subs','subs'],
    ['/test-mount/','events']]);

  h.context.location.pathname=links[0][0];
  h.events.popstate();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(h.elements.get('eventsPage').hidden,false);
  assert.equal(h.elements.get('subsPage').hidden,true);
  assert.ok(calls.every(path=>path.startsWith('/test-mount/api/')));
});

for (const retry of ['online', 'tab', 'query']) {
  test(`failed initial area load can retry via ${retry}`,async()=>{
    const h=harness();
    let areaRequests=0;
    const tournamentAreas=[];
    h.context.fetch=async path=>{
      const url=new URL(path,'https://example.com');
      if(url.pathname==='/api/areas') {
        if(++areaRequests===1) {
          if(retry==='online') throw Error('offline');
          return {json:async()=>({ok:true,provinces:[]})};
        }
        return {json:async()=>({ok:true,provinces:[{name:'广东省',cities:['广州市']}]})};
      }
      if(url.pathname==='/api/tournaments') {
        tournamentAreas.push(url.searchParams.get('area'));
        return {json:async()=>({ok:true,account_id:'10001',items:[]})};
      }
      return {json:async()=>state()};
    };
    // Simulate the browser selecting the first option assigned by loadAreas().
    Object.defineProperty(h.run('$("province")'),'selectedIndex',{
      set(){this.value='广东省';}
    });
    h.context.window.scrollTo=()=>{};
    const show=async pathname=>{
      h.context.location.pathname=pathname;
      h.events.popstate();
      await new Promise(resolve=>setImmediate(resolve));
    };
    await show('/');
    assert.equal(areaRequests,1);
    assert.deepEqual(tournamentAreas,[]);
    assert.match(h.elements.get('listInfo').textContent,/暂时无法加载省市/);
    if(retry==='online') await h.events.online();
    if(retry==='tab') {
      await show('/subs');
      await show('/');
    }
    if(retry==='query') await h.elements.get('btnLoad').onclick();
    assert.equal(areaRequests,2);
    assert.deepEqual(tournamentAreas,['广东省-广州市']);
  });
}

for (const retry of ['online', 'query', 'after scrolling']) {
  test(`failed event refresh restores cards, picks and scroll on ${retry}`,async()=>{
    const h=harness();
    const event={tournament_id:'test-event',name:'test-event',group:'open'};
    h.context.oldTours=[event,{...event,tournament_id:'test-removed'}];
    h.run(`currentPage="events"; notifyStarted=true; AREAS=[{name:"测试省",cities:["测试市"]}];
      $("province").value="测试省"; $("city").value="测试市";
      TOURS=oldTours; PICK_ACCTS.add("10001");
      PICK_TOURS.add("test-event"); PICK_TOURS.add("test-removed"); renderSections();`);
    let failed=false;
    let batchRequests=0;
    let tournamentRequests=0;
    h.context.fetch=async path=>{
      if(path.startsWith('/api/tournaments')) {
        tournamentRequests++;
        if(failed && retry==='online') throw Error('offline');
        return {json:async()=>failed
          ? {ok:false,error:'临时查询失败'}
          : {ok:true,account_id:'10001',items:[event]}};
      }
      if(path.startsWith('/api/batch_enroll')) batchRequests++;
      return {json:async()=>state()};
    };
    let scrollY=600;
    const window=h.context.window;
    Object.defineProperty(window,'scrollY',{get(){
      if(!h.elements.get('eventsPage').hidden &&
          !h.elements.get('tourList').innerHTML.includes('test-event')) scrollY=Math.min(scrollY,200);
      return scrollY;
    }});
    window.scrollTo=(_x,y)=>{scrollY=y;};
    const show=async pathname=>{
      h.context.location.pathname=pathname;
      h.events.popstate();
      await new Promise(resolve=>setImmediate(resolve));
    };
    await show('/subs');
    failed=true;
    await show('/');

    assert.match(h.elements.get('listInfo').textContent,/临时查询失败|offline/);
    assert.doesNotMatch(h.elements.get('tourList').innerHTML,/test-event/);
    assert.equal(window.scrollY,200);
    assert.equal(h.run('PICK_TOURS.size'),2);
    assert.equal(h.elements.get('btnBatch').disabled,true);
    await h.run('batchEnroll()');
    assert.equal(batchRequests,0);

    const moved=retry==='after scrolling';
    if(moved) {
      scrollY=50;
      h.events.scroll();
    }
    // Another round trip keeps the old target only if the user hasn't moved.
    await show('/subs');
    await show('/');
    assert.equal(window.scrollY,moved ? 50 : 200);
    failed=false;
    if(retry==='online') await h.events.online();
    else await h.elements.get('btnLoad').onclick();
    assert.equal(tournamentRequests,3);
    assert.match(h.elements.get('tourList').innerHTML,/data-id="test-event" checked/);
    assert.equal(h.run('PICK_TOURS.has("test-removed")'),false);
    assert.equal(h.elements.get('btnBatch').disabled,false);
    assert.equal(window.scrollY,moved ? 50 : 600);
  });
}

test('a failed post-operation event reload keeps the query error through redraws',async()=>{
  const h=harness();
  h.run('ACCOUNT_ID="10001"; $("province").value="测试省"; $("city").value="测试市";');
  let failed=true;
  h.context.fetch=async path=>{
    if(!path.startsWith('/api/tournaments')) return {json:async()=>state()};
    if(failed) throw Error('test offline');
    return {json:async()=>({ok:true,account_id:'10001',items:[]})};
  };
  await h.run('reloadAll()');
  const info=h.elements.get('listInfo');
  const myInfo=h.elements.get('myInfo');
  assert.match(info.textContent,/test offline/);
  assert.match(myInfo.textContent,/test offline/);
  h.run('renderSections()');
  assert.match(info.textContent,/test offline/);
  failed=false;
  await h.run('reloadAll()');
  assert.match(info.textContent,/共 0 场/);
  assert.match(myInfo.textContent,/没有已订阅或已报名的赛事/);
});

test('return to top clears a saved events offset and respects reduced motion',()=>{
  const h=harness();
  const button=h.elements.get('backToTop');
  const {window,document}=h.context;
  const scrollCalls=[];
  const focusCalls=[];
  window.scrollY=220;
  window.matchMedia=query=>({matches:query==='(prefers-reduced-motion: reduce)'});
  window.scrollTo=options=>{scrollCalls.push(options);window.scrollY=options.top;};
  document.activeElement=button;
  h.run('currentPage="events"; pendingEventsScroll={offset:600, observed:220};');
  h.run('$("appTitle")').focus=options=>focusCalls.push(options.preventScroll);

  h.events.scroll();
  assert.equal(button.hidden,false);
  button.onclick();
  assert.equal(h.run('pendingEventsScroll'),null);
  assert.equal(scrollCalls[0].top,0);
  assert.equal(scrollCalls[0].behavior,'auto');
  assert.equal(button.hidden,true);
  assert.deepEqual(focusCalls,[true]);
});

function pullHarness(page) {
  const h=harness();
  h.context.window.scrollY=0;
  h.run(`currentPage="${page}"; ACCOUNT_ID="10001";
    AREAS=[{name:"测试省",cities:["测试市"]}];
    $("province").value="测试省"; $("city").value="测试市";`);
  h.run('$("appTitle")').parentElement={getBoundingClientRect:()=>({bottom:57})};
  const ptr=h.elements.get('ptr');
  ptr.style.setProperty=()=>{};
  const timers=new Map();
  h.context.setTimeout=(fn,ms)=>{const key=Symbol();timers.set(key,{fn,ms});return key;};
  h.context.clearTimeout=key=>timers.delete(key);
  const touch=(name,y,{excluded=false,x=0,count=1}={})=>{
    let prevented=false;
    h.events[name]({
      touches:Array.from({length:count},()=>({clientY:y,clientX:x})),
      target:{closest:()=>excluded},cancelable:true,preventDefault(){prevented=true;}
    });
    return prevented;
  };
  const pull=()=>{
    touch('touchstart',80);
    touch('touchmove',240);
    touch('touchend',240);
  };
  return {...h,ptr,timers,touch,pull};
}

test('pull ignores short, reversed, cancelled, horizontal, multi-touch and inner scrolling gestures',()=>{
  const h=pullHarness('events');
  const {touch,ptr}=h;
  let calls=0;
  h.context.fetch=()=>{calls++;throw Error('unexpected request');};
  touch('touchstart',80);
  assert.equal(touch('touchmove',120),true);
  assert.equal(ptr.dataset.state,'pulling');
  touch('touchend',120);
  assert.equal(ptr.dataset.state,undefined);

  touch('touchstart',80);
  touch('touchmove',240);
  assert.equal(ptr.dataset.state,'ready');
  touch('touchmove',79);
  assert.equal(ptr.dataset.state,undefined);
  touch('touchend',79);

  touch('touchstart',80);
  touch('touchmove',240);
  touch('touchcancel',240);
  touch('touchend',240);

  touch('touchstart',80);
  touch('touchmove',240);
  touch('touchstart',240,{count:2});
  touch('touchend',240);

  touch('touchstart',80);
  assert.equal(touch('touchmove',110,{x:80}),false);
  touch('touchend',110);

  touch('touchstart',80,{excluded:true});
  assert.equal(touch('touchmove',240),false);
  touch('touchend',240);

  h.context.window.scrollY=10;
  h.pull();
  assert.equal(ptr.dataset.state,undefined);
  assert.equal(calls,0);
});

for (const page of ['events','subs']) {
  test(`a pull on ${page} issues one manual refresh and waits for the active tab's data`,async()=>{
    const h=pullHarness(page);
    const calls=[];
    let release;
    h.context.fetch=(url,opts)=>{
      const path=new URL(url,'https://sve.example.com').pathname;
      calls.push(path);
      assert.ok(opts.signal);
      if (path==='/api/state') return new Promise(resolve=>{release=resolve;});
      return Promise.resolve({json:async()=>({ok:true,account_id:'10001',items:[]})});
    };
    h.pull();
    assert.equal(h.ptr.dataset.state,'busy');
    h.pull();
    assert.deepEqual(calls,['/api/state']);
    release({json:async()=>state()});
    await new Promise(resolve=>setImmediate(resolve));
    assert.deepEqual(calls,page==='events'?['/api/state','/api/tournaments']:['/api/state']);
    assert.equal(h.ptr.dataset.state,undefined);
  });
}

test('a timed-out pull aborts its request and a failed retry releases the indicator',async()=>{
  const h=pullHarness('events');
  let signal;
  h.context.fetch=(_url,opts)=>{
    signal=opts.signal;
    return new Promise((_resolve,reject)=>signal.addEventListener('abort',()=>reject(Error('aborted'))));
  };
  h.pull();
  const [timer]=h.timers.values();
  assert.equal(timer.ms,30000);
  timer.fn();
  assert.equal(signal.aborted,true);
  assert.equal(h.ptr.dataset.state,undefined);
  assert.match(h.elements.get('ptrStatus').textContent,/超时/);
  await new Promise(resolve=>setImmediate(resolve));

  const calls=[];
  h.context.fetch=async url=>{calls.push(url);throw Error('offline');};
  h.pull();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(h.ptr.dataset.state,undefined);
  assert.equal(calls.length,2);
  assert.match(h.elements.get('connectionStatus').textContent,/网络不可用/);
});

test('switching tabs aborts the old pull without reporting a connection failure',async()=>{
  const h=pullHarness('events');
  h.context.window.scrollTo=()=>{};
  let signal;
  const calls=[];
  h.context.fetch=(url,opts)=>{
    calls.push(new URL(url,'https://sve.example.com').pathname);
    if (!opts.signal) return Promise.resolve({json:async()=>state()});
    signal=opts.signal;
    return new Promise((_resolve,reject)=>signal.addEventListener('abort',()=>reject(Error('aborted'))));
  };
  h.pull();
  assert.equal(h.ptr.dataset.state,'busy');
  h.run('location.pathname="/subs"; showLocation();');
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(signal.aborted,true);
  assert.equal(h.ptr.dataset.state,undefined);
  assert.deepEqual(calls,['/api/state','/api/state']);
  assert.match(h.elements.get('summary').textContent,/共 0 条/);
  assert.equal(h.elements.get('connectionStatus').style.display,'none');
});

test('changing city after a failed query keeps the current scroll position',async()=>{
  const h=harness();
  const window=h.context.window;
  window.scrollY=600;
  window.scrollTo=(_x,y)=>{window.scrollY=y;};
  h.run(`currentPage="events"; ACCOUNT_ID="10001";
    AREAS=[{name:"测试省",cities:["旧测试市","新测试市"]}];
    $("province").value="测试省"; $("city").value="旧测试市";`);
  const areas=[];
  h.context.fetch=async path=>{
    const area=new URL(path,'https://example.com').searchParams.get('area');
    areas.push(area);
    return {json:async()=>area==='测试省-旧测试市'
      ? {ok:false,error:'临时查询失败'}
      : {ok:true,account_id:'10001',items:[
        {tournament_id:'test-new-event',name:'test-new-event',group:'open'}]}};
  };
  await h.elements.get('btnLoad').onclick();
  assert.match(h.elements.get('listInfo').textContent,/临时查询失败/);

  window.scrollY=50;
  const city=h.elements.get('city');
  city.value='新测试市';
  await city.onchange();

  assert.deepEqual(areas,['测试省-旧测试市','测试省-新测试市']);
  assert.match(h.elements.get('tourList').innerHTML,/test-new-event/);
  assert.equal(window.scrollY,50);
});

test('manual profile refresh sends the clicked ID and its version even after another account is selected',async()=>{
  const h=harness();
  h.run('ACCOUNT_ID="10002"; ACCOUNT_ROWS=new Map([["10001",{version:7}]]);');
  const calls=[];
  h.context.fetch=async(url,opts)=>{
    calls.push({url,body:opts.body && JSON.parse(opts.body)});
    return {json:async()=>url.startsWith('/api/state')?state('test-renamed'):{ok:true}};
  };
  await h.run('refreshProfile("10001")');
  assert.match(calls[0].url,/^\/api\/account\/profile\/refresh/);
  assert.deepEqual(calls[0].body,{account_id:'10001',expected_version:7});
  assert.match(h.elements.get('activeNickname').textContent,/test-renamed/);
});

test('offline keeps account data with a stale notice; reconnect and visibility refresh from server',async()=>{
  const h=harness();
  h.context.fetch=async()=>({json:async()=>state('test-old')});
  await h.run('refresh()');
  h.context.fetch=async()=>{throw Error('offline');};
  await h.run('refresh()');
  assert.match(h.elements.get('activeNickname').textContent,/test-old/);
  assert.match(h.elements.get('connectionStatus').textContent,/网络不可用/);
  h.context.fetch=async()=>({json:async()=>state('test-new')});
  h.events.online();
  await new Promise(resolve=>setImmediate(resolve));
  assert.match(h.elements.get('activeNickname').textContent,/test-new/);
  assert.equal(h.elements.get('connectionStatus').style.display,'none');
  assert.equal(typeof h.events.visibilitychange,'function');
});

test('late state response cannot roll a nickname back and existing result labels follow current profiles',async()=>{
  const h=harness();
  const label={textContent:'test-old',dataset:{accountLabel:'10001'}};
  h.context.document.querySelectorAll=selector=>selector==='[data-account-label]'?[label]:[];
  let release;
  h.context.fetch=()=>new Promise(resolve=>{release=resolve;});
  const pending=h.run('refresh()');
  h.context.fetch=async()=>({json:async()=>state('test-new')});
  await h.run('refresh()');
  release({json:async()=>state('test-old')});
  await pending;
  assert.match(h.elements.get('activeNickname').textContent,/test-new/);
  assert.match(label.textContent,/test-new/);
});

test('subscription headings update after rename without replacing cards; late polls are ignored',async()=>{
  const h=harness();
  const groups=h.run('$("groups")');
  groups.querySelectorAll=selector=>selector==='.acct-group h2' ? groups.children.map(group=>group.children[0]) : [];
  const accountName=()=>groups.children[0].children[0].firstElementChild.textContent;
  const response=name=>({ok:true,sub_count:1,subs_grouped:{'10001':[{key:'10001|event',account:name}]}});
  let release;
  h.context.fetch=()=>new Promise(resolve=>{release=resolve;});
  const pending=h.run('refresh()');
  h.context.fetch=async()=>({json:async()=>response('test-new (UID 10001)')});
  await h.run('refresh()');
  release({json:async()=>response('test-old (UID 10001)')});
  await pending;
  assert.equal(accountName(),'test-new (UID 10001)');
  const card=groups.children[0].children[1].children[0];
  h.context.fetch=async()=>({json:async()=>response('test-renamed (UID 10001)')});
  await h.run('refresh()');
  assert.equal(accountName(),'test-renamed (UID 10001)');
  assert.equal(groups.children[0].children[1].children[0],card);
  h.context.fetch=async()=>{throw Error('offline');};
  await h.run('refresh()');
  assert.match(h.elements.get('summary').textContent,/网络不可用/);
  assert.equal(accountName(),'test-renamed (UID 10001)');
});

test('subscription slow polls keep updating while newer requests are pending',async()=>{
  const h=harness();
  const groups=h.run('$("groups")');
  groups.querySelectorAll=selector=>selector==='.acct-group h2' ? groups.children.map(group=>group.children[0]) : [];
  const accountName=()=>groups.children[0].children[0].firstElementChild.textContent;
  const releases=[];
  h.context.fetch=()=>new Promise((resolve,reject)=>releases.push({resolve,reject}));
  const pending=[h.run('refresh()'),h.run('refresh()')];
  for(let i=0;i<3;i++) {
    pending.push(h.run('refresh()'));
    releases[i].resolve({json:async()=>({ok:true,sub_count:1,
      subs_grouped:{'10001':[{key:'10001|event',account:'test-slow-'+i}]}})});
    await pending[i];
    assert.equal(accountName(),'test-slow-'+i);
  }
  // A failed slow request must also report offline while another poll is pending.
  releases[3].reject(Error('offline'));
  await pending[3];
  assert.match(h.elements.get('summary').textContent,/网络不可用/);
  assert.equal(accountName(),'test-slow-2');
  releases[4].resolve({json:async()=>({ok:true,subs_grouped:{}})});
  await pending[4];
  assert.match(h.elements.get('summary').textContent,/共 0 条订阅/);
});

test('home slow polls keep rendering accounts despite continuously pending newer polls',async()=>{
  const h=harness();
  const releases=[];
  h.context.fetch=()=>new Promise((resolve,reject)=>releases.push({resolve,reject}));
  const pending=[h.run('refresh()'),h.run('refresh()')];
  for(let i=0;i<4;i++) {
    pending.push(h.run('refresh()'));
    releases[i].resolve({json:async()=>state('test-slow-'+i)});
    await pending[i];
    assert.equal(h.run('ACCOUNT_ROWS.size'),2);
    assert.match(h.elements.get('activeNickname').textContent,new RegExp('test-slow-'+i));
  }
  releases[4].reject(Error('offline'));
  await pending[4];
  assert.equal(h.elements.get('connectionStatus').style.display,'block');
  assert.match(h.elements.get('activeNickname').textContent,/test-slow-3/);
  releases[5].resolve({json:async()=>state('test-online')});
  await pending[5];
  assert.equal(h.elements.get('connectionStatus').style.display,'none');
  assert.match(h.elements.get('activeNickname').textContent,/test-online/);
});

test('late home failures and successes cannot supersede newer settled results',async()=>{
  for (const failure of [true,false]) {
    const h=harness();
    let resolve,reject;
    h.context.fetch=()=>new Promise((ok,fail)=>{resolve=ok;reject=fail;});
    const old=h.run('refresh()');
    h.context.fetch=async()=>({json:async()=>state('test-new')});
    await h.run('refresh()');
    if(failure) reject(Error('offline'));
    else resolve({json:async()=>state('test-old')});
    await old;
    assert.match(h.elements.get('activeNickname').textContent,/test-new/);
    assert.equal(h.elements.get('connectionStatus').style.display,'none');
  }
});

test('home responses from a prior account epoch cannot restore the old account',async()=>{
  const h=harness();
  h.run('ACCOUNT_ID="10001"; LAST_ACTIVE="10001";');
  let resolve;
  h.context.fetch=()=>new Promise(ok=>{resolve=ok;});
  const old=h.run('refresh()');
  h.run('ACCOUNT_ID="10002"; ACCOUNT_EPOCH++;');
  resolve({json:async()=>state('test-old-account')});
  await old;
  assert.equal(h.run('ACCOUNT_ID'),'10002');
  assert.equal(h.run('ACCOUNT_ROWS.size'),0);
});

test('a late subscription poll failure cannot replace a newer successful status',async()=>{
  const h=harness();
  let reject;
  h.context.fetch=()=>new Promise((resolve,fail)=>{reject=fail;});
  const pending=h.run('refresh()');
  h.context.fetch=async()=>({json:async()=>({ok:true,subs_grouped:{}})});
  await h.run('refresh()');
  reject(Error('offline'));
  await pending;
  assert.match(h.elements.get('summary').textContent,/共 0 条订阅/);
});

test('PWA API requests always use the network and the new shell caches the legacy subscription route',()=>{
  const handlers={};
  let fetches=0;
  const source=fs.readFileSync('app/client/ui/sw.js','utf8');
  const c=vm.createContext({URL,self:{location:{origin:'https://sve.example.com'},
    registration:{scope:'https://sve.example.com/test-mount/'},
    addEventListener(name,fn){handlers[name]=fn;}},fetch(){fetches++;return Promise.resolve('network');}});
  vm.runInContext(source,c);
  let response;
  for (const path of ['/api/state','/test-mount/api/state']) {
    handlers.fetch({request:{method:'GET',url:'https://sve.example.com'+path},respondWith(value){response=value;}});
    assert.ok(response instanceof Promise);
  }
  assert.equal(fetches,2);
  assert.notEqual(vm.runInContext('VERSION',c),'sve-v9-predeployment');
  assert.ok(vm.runInContext('SHELL.includes("./subs")',c));
  assert.ok(vm.runInContext('SHELL.includes("./settings")',c));
});

function notificationConfig() {
  return {ok:true,file:'/var/lib/sve/notify.json',channels:{
    pushplus:{enabled:false,token__set:false},
    email:{enabled:false,use_ssl:true,smtp_host:'smtp.example.com',smtp_port:465}}};
}

for (const id of ['123456','123456'+'7890','123456'+'78901234567890']) {
  test(`the account header displays the full string ID with ${id.length} digits`,async()=>{
    const h=harness();
    const nickname='test-<name>&"';
    h.context.fetch=async()=>({json:async()=>({...state(),active_account_id:id,
      accounts:[{account_id:id,uid:id,nickname,version:1}]})});
    await h.run('refresh()');
    assert.equal(h.elements.get('activeNickname').textContent,nickname);
    assert.equal(h.elements.get('activeNickname').title,nickname);
    assert.equal(h.elements.get('activeUid').textContent,`(${id})`);
    assert.equal(h.elements.get('activeUid').hidden,false);
    assert.equal(h.run(`accountLabel(${JSON.stringify(id)})`),`${nickname} (UID ${id})`);
  });
}

test('the account header changes ID immediately on selection and clears an invalidated identity',async()=>{
  const h=harness();
  h.context.fetch=async()=>({json:async()=>state('test-shared')});
  await h.run('refresh()');
  assert.equal(h.elements.get('activeUid').textContent,'(10001)');
  let release;
  h.context.fetch=path=>path.includes('/api/account/select')?
    new Promise(resolve=>release=resolve):Promise.resolve({json:async()=>({...state('test-shared'),active_account_id:'10002'})});
  const selecting=h.run('selectAcct("10002")');
  assert.equal(h.elements.get('activeNickname').textContent,'test-shared');
  assert.equal(h.elements.get('activeUid').textContent,'(10002)');
  release({json:async()=>({ok:true})});await selecting;
  h.context.fetch=async()=>({json:async()=>({ok:false,code:'identity_conflict'})});
  await h.run('refresh()');
  assert.equal(h.elements.get('activeNickname').textContent,'未登录');
  assert.equal(h.elements.get('activeUid').textContent,'');
  assert.equal(h.elements.get('activeUid').hidden,true);
});

test('the GitHub entry uses plain repository metadata and rejects non-repository URLs',()=>{
  const h=harness();
  const base='https://github.com/example/sve-monitor';
  const link=h.elements.get('projectGithub');
  h.context.document.querySelector=()=>({content:base});
  h.run('configureProjectLink()');
  assert.equal(link.href,base);
  assert.equal(link.hidden,false);
  for(const url of ['',base+'/issues',base+'?x=1',base+'#readme',base.replace('github.com','example.com'),
    base.replace('https:','http:')]) {
    h.context.document.querySelector=()=>({content:url});
    h.run('configureProjectLink()');
    assert.equal(link.hidden,true);
  }
});

for (const pathname of ['/settings','/test-mount/settings']) {
  test(`settings deep link ${pathname} reads state/config only and recovers online`,async()=>{
    const h=harness(pathname),calls=[];
    h.context.fetch=async(path,options)=>{
      calls.push([path,options?.method||'GET']);
      return {json:async()=>path.includes('/api/state')?state():notificationConfig()};
    };
    h.run('showLocation()');
    await new Promise(resolve=>setImmediate(resolve));
    assert.equal(h.run('currentPage'),'settings');
    assert.equal(h.elements.get('settingsPage').hidden,false);
    assert.equal(h.elements.get('eventsPage').hidden,true);
    assert.equal(h.elements.get('subsPage').hidden,true);
    const prefix=pathname.startsWith('/test-mount/')?'/test-mount':'';
    assert.deepEqual(calls.map(([path,method])=>[path.split('?')[0],method]),[
      [prefix+'/api/state','GET'],[prefix+'/api/notify/config','GET']]);
    assert.match(h.elements.get('notifyForm').innerHTML,/nf_email_smtp_host/);
    await h.events.online();
    h.events.visibilitychange();
    await new Promise(resolve=>setImmediate(resolve));
    assert.ok(calls.every(([path,method])=>method==='GET'&&!path.includes('/tournaments')));
    assert.equal(h.elements.get('settingsConnectionStatus').style.display,'none');
  });
}

test('business navigation does not load notification configuration and settings roundtrip preserves picks and scroll',async()=>{
  const h=harness();
  const calls=[];
  h.context.fetch=async(path,options)=>{
    calls.push([path,options?.method||'GET']);
    return {json:async()=>path.includes('/api/state')?state():path.includes('/api/notify/config')?
      notificationConfig():{ok:true,account_id:'10001',items:[{tournament_id:'one',name:'test-event',group:'open'}]}};
  };
  h.run(`currentPage="events"; ACCOUNT_ID="10001"; LAST_ACTIVE="10001";
    AREAS=[{name:"test-province",cities:["test-city"]}];
    $("province").value="test-province"; $("city").value="test-city";
    PICK_ACCTS.add("10001"); PICK_TOURS.add("one");`);
  h.context.window.scrollY=420;
  h.context.location.pathname='/settings';
  h.run('showLocation()');
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(h.run('scrollPositions.events'),420);
  h.context.location.pathname='/';
  const restored=[];
  h.context.window.scrollTo=(...args)=>restored.push(args);
  h.run('showLocation()');
  await new Promise(resolve=>setImmediate(resolve));
  assert.ok(restored.some(args=>args[1]===420));
  assert.equal(h.elements.get('province').value,'test-province');
  assert.equal(h.run('PICK_ACCTS.has("10001") && PICK_TOURS.has("one")'),true);
  calls.length=0;
  h.context.location.pathname='/subs';h.run('showLocation()');
  await new Promise(resolve=>setImmediate(resolve));
  assert.ok(calls.every(([path,method])=>method==='GET'&&!path.includes('/notify/')));
});

test('settings config failures are visible, preserve edits, and late reads cannot replace newer settings',async()=>{
  const h=harness('/settings');
  h.run('currentPage="settings";');
  h.context.fetch=async()=>({json:async()=>notificationConfig()});
  await h.run('loadNotifyConfig()');
  const markup=h.elements.get('notifyForm').innerHTML;
  h.elements.get('notifyForm').events.input();
  h.context.fetch=async()=>{throw Error('synthetic-offline');};
  await h.run('loadNotifyConfig();');await h.run('refresh()');
  assert.match(h.elements.get('notifyBox').innerHTML,/无法取得最新通知设置/);
  assert.match(h.elements.get('settingsConnectionStatus').textContent,/尚未确认/);
  assert.equal(h.elements.get('notifyForm').innerHTML,markup);
  let release;
  h.context.fetch=()=>new Promise(resolve=>release=resolve);
  const older=h.run('loadNotifyConfig()');
  h.context.fetch=async()=>({json:async()=>({...notificationConfig(),channels:{email:{enabled:true}}})});
  await h.run('loadNotifyConfig()');
  release({json:async()=>({...notificationConfig(),channels:{email:{enabled:false}}})});await older;
  assert.match(h.elements.get('notifyBox').innerHTML,/邮件：已启用/);
  assert.doesNotMatch(h.elements.get('notifyBox').innerHTML,/邮件：未启用/);
  assert.equal(h.elements.get('notifyForm').innerHTML,markup);
});

test('settings actions use their existing APIs and show feedback in settings',async()=>{
  const h=harness('/settings'),calls=[];
  h.run('currentPage="settings"; $("rmHost").value="server.example.com"; $("rmUser").value="test-user";');
  h.context.fetch=async(path,options)=>{
    calls.push([path.split('?')[0],options?.method||'GET']);
    return {json:async()=>path.includes('/api/state')?state():path.includes('/notify/config')?
      notificationConfig():{ok:true,results:[],local_only:[],remote_only:[],conflicts:[]}};
  };
  await h.elements.get('btnRemoteOn').onclick();
  await h.elements.get('btnRemoteOff').onclick();
  await h.elements.get('btnPushAccounts').onclick();
  await h.elements.get('btnNotifySave').onclick();
  assert.match(h.elements.get('settingsActionStatus').textContent,/通知配置已保存/);
  await h.elements.get('btnNotifyTest').onclick();
  assert.match(h.elements.get('settingsActionStatus').textContent,/测试通知已发送/);
  assert.deepEqual(calls.filter(([,method])=>method==='POST').map(([path])=>path),[
    '/api/remote/config','/api/remote/config','/api/remote/push_accounts','/api/notify/config','/api/notify/test']);
  h.context.fetch=async()=>{throw Error('synthetic-offline');};
  await h.elements.get('btnNotifySave').onclick();
  assert.match(h.elements.get('settingsActionStatus').textContent,/未确认保存/);
  assert.equal(h.elements.get('btnNotifySave').disabled,false);
  await h.elements.get('btnPushAccounts').onclick();
  assert.equal(h.elements.get('btnPushAccounts').disabled,false);
});

test('settings markup and messages discard legacy configuration values and secret fragments',()=>{
  const h=harness();
  h.context.privateValue='test-private-marker';
  h.run(`NOTIFY_CFG={channels:{email:{enabled:true,smtp_host:privateValue,authcode:privateValue,
    authcode__set:true,username:privateValue,username__source:"env",use_ssl:true}}};
    renderNotifyForm(); renderNotifySummary({...NOTIFY_CFG,file:privateValue});
    renderRemoteStatus({enabled:true,user:privateValue,host:privateValue,error:privateValue});`);
  for (const id of ['notifyForm','notifyBox','remoteBox','remoteFieldsStatus']) {
    assert.ok(!h.elements.get(id).innerHTML.includes('test-private-marker'));
    assert.ok(!h.elements.get(id).textContent.includes('test-private-marker'));
  }
  assert.match(h.elements.get('notifyForm').innerHTML,/type="password"/);
  assert.match(h.elements.get('notifyForm').innerHTML,/value=""/);
  assert.match(h.elements.get('notifyForm').innerHTML,/环境变量，只读/);
  assert.equal(h.run('settingsMessage({error:privateValue},"test-generic")'),'test-generic');
});

test('blank remote submissions rely on server merge and keep newer edits during a slow save',async()=>{
  const h=harness('/settings');let release;const bodies=[];
  h.run('currentPage="settings";');
  h.context.fetch=(path,options)=>{
    if(options?.method==='POST') {
      bodies.push(JSON.parse(options.body));return new Promise(resolve=>release=resolve);
    }
    return Promise.resolve({json:async()=>state()});
  };
  const pending=h.elements.get('btnRemoteOn').onclick();
  await h.elements.get('btnRemoteOn').onclick();
  assert.deepEqual(bodies,[{enabled:true}]);
  h.run('$("rmHost").value="new.example.com"; remoteEditVersion++;');
  release({json:async()=>({ok:true})});await pending;
  assert.equal(h.elements.get('rmHost').value,'new.example.com');
  await h.run('refresh()');
  assert.equal(h.elements.get('rmHost').value,'new.example.com');
  h.run('resetSettingsInputs()');
  assert.equal(h.elements.get('rmHost').value,'');
});

test('a cached settings-page return clears input while an ordinary foreground refresh preserves it',async()=>{
  const h=harness('/settings');
  h.context.fetch=async path=>({json:async()=>path.includes('/api/state')?state():notificationConfig()});
  h.run('currentPage="settings"; $("rmHost").value="new.example.com";');
  h.events.visibilitychange();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(h.elements.get('rmHost').value,'new.example.com');
  h.events.pageshow({persisted:true});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(h.elements.get('rmHost').value,'');
});

test('notification save keeps newer typing, drops unsafe errors and clears submitted inputs on success',async()=>{
  const h=harness('/settings');let release;
  h.context.fetch=()=>new Promise(resolve=>release=resolve);
  h.run('$("nf_email_smtp_host").value="smtp.example.com";');
  const pending=h.elements.get('btnNotifySave').onclick();
  h.elements.get('notifyForm').events.input();
  release({json:async()=>notificationConfig()});await pending;
  assert.equal(h.run('notifyFormDirty'),true);
  h.context.fetch=async()=>({json:async()=>({ok:false,error:'test-private-diagnostic'})});
  await h.elements.get('btnNotifySave').onclick();
  assert.doesNotMatch(h.elements.get('settingsActionStatus').textContent,/test-private-diagnostic/);
  h.context.fetch=async()=>({json:async()=>notificationConfig()});
  await h.elements.get('btnNotifySave').onclick();
  assert.equal(h.run('notifyFormDirty'),false);
  assert.match(h.elements.get('notifyForm').innerHTML,/value=""/);
});

test('same nicknames render ID handlers, escaped text, and stable batch selection after rename',async()=>{
  const h=harness();
  let response=state('test-\'"<img src=x onerror=alert(1)>|');
  h.context.fetch=async()=>({json:async()=>response});
  await h.run('refresh()');
  const rendered=h.elements.get('acctList').innerHTML;
  assert.match(rendered,/selectAcct\('10002'\)/);
  assert.match(rendered,/data-id="10001"/);
  assert.ok(!rendered.includes('<img'));
  h.run('PICK_ACCTS.add("10001")');
  response=state('test-renamed');
  await h.run('refresh()');
  assert.equal(h.run('PICK_ACCTS.has("10001")'),true);
  assert.equal(h.elements.get('activeNickname').textContent,'test-renamed');
  assert.equal(h.elements.get('activeUid').textContent,'(10001)');
});

test('account polling preserves the inner scroll and only restores valid selections',async()=>{
  const h=harness();
  let response=state();
  h.context.fetch=async()=>({json:async()=>response});
  await h.run('refresh()');

  const list=h.elements.get('acctList');
  const first={checked:false,dataset:{id:'10001'}};
  const second={checked:false,dataset:{id:'10002'}};
  list.querySelectorAll=()=>[first,second];
  list.scrollTop=180;
  h.run('PICK_ACCTS.add("10001"); PICK_ACCTS.add("10002");');
  response=state('test-renamed');
  response.accounts[0].profile_refreshing=true;
  await h.run('refresh()');
  assert.equal(list.scrollTop,180);
  assert.ok(first.checked && second.checked);
  assert.match(list.innerHTML,/class="small acct-refresh"[^>]* disabled>刷新中/);

  response=state('test-renamed');
  response.accounts.pop();
  list.querySelectorAll=()=>[first];
  await h.run('refresh()');
  assert.equal(list.scrollTop,180);
  assert.equal(first.checked,true);
  assert.deepEqual(Array.from(h.run('PICK_ACCTS')),['10001']);
});

test('late tournament response cannot populate another selected account',async()=>{
  const h=harness();
  h.run('ACCOUNT_ID="10001"; ACCOUNT_ROWS=new Map([["10001",{version:1}]]);');
  h.elements.get('province').value='test-province';
  h.elements.get('city').value='test-city';
  let release;
  h.context.fetch=()=>new Promise(resolve=>{release=resolve;});
  const pending=h.run('loadTournaments()');
  h.run('ACCOUNT_ID="10002"; ACCOUNT_EPOCH++;');
  release({json:async()=>({ok:true,account_id:'10001',items:[{tournament_id:'old'}]})});
  await pending;
  assert.equal(h.run('TOURS.length'),0);
});

test('action is pinned to its original ID and late success cannot mark another account enrolled',async()=>{
  const h=harness();
  h.run('ACCOUNT_ID="10001"; ACCOUNT_ROWS=new Map([["10001",{version:1}]]);');
  let release, sent;
  h.context.fetch=(url,opts)=>{sent=JSON.parse(opts.body);return new Promise(resolve=>{release=resolve;});};
  const pending=h.run('enroll("test-event")');
  assert.equal(sent.account_id,'10001');
  assert.equal(sent.expected_version,1);
  h.run('ACCOUNT_ID="10002"; ACCOUNT_EPOCH++; PENDING.clear(); ENROLLED.clear();');
  release({json:async()=>({ok:true})});
  await pending;
  assert.equal(h.run('ENROLLED.size'),0);
});

test('an unknown or deleted ID never silently retains a stale tournament view',async()=>{
  const h=harness();
  h.run('ACCOUNT_ID="10001"; TOURS=[{tournament_id:"old"}];');
  h.context.fetch=async()=>({json:async()=>({ok:false,code:'identity_conflict'})});
  await h.run('refresh()');
  assert.equal(h.run('ACCOUNT_ID'),null);
  assert.equal(h.run('TOURS.length'),0);
});

test('installed native login keeps its URL scheme even when the injected version is ignored',async()=>{
  const h=harness();
  h.context.window.__SVE_APP__=true;
  h.context.window.__SVE_VER__={name:'test-native-version',code:100};
  h.context.fetch=async()=>({json:async()=>state()});
  await h.run('refresh()');
  assert.equal(h.elements.get('btnLogin').style.display,'');
  assert.equal(h.elements.has('verLine'),false);
  const calls=[];
  h.context.fetch=async(url,opts)=>{calls.push({url,body:JSON.parse(opts.body)});return {json:async()=>({ok:true})};};
  await h.run('startLogin({restore_account_id:"10001",expected_version:2})');
  assert.equal(calls.length,1);
  assert.match(calls[0].url,/^\/api\/login\/intent/);
  assert.equal(calls[0].body.restore_account_id,'10001');
  assert.equal(h.context.location.href,'sve://login');
});

test('deleting the selected account clears both views before loading the remaining account',async()=>{
  const h=harness();
  h.run(`ACCOUNT_ID="10001"; LAST_ACTIVE="10001";
    ACCOUNT_ROWS=new Map([["10001",{version:1}],["10002",{version:1}]]);
    TOURS=[{tournament_id:"old"}]; MY_TOURS=[{tournament_id:"old"}];
    PENDING.set("old","subscribe"); CONFIRMED.set("old",{want:true,until:Date.now()+10000});
    ENROLLED.set("old",{}); MINE.add("old"); PICK_TOURS.add("old");`);
  let release;
  const loaded=[];
  h.context.loaded=loaded;
  h.run('loadTournaments=async()=>{loaded.push(ACCOUNT_ID);};');
  h.context.fetch=async(url)=>{
    if(url.startsWith('/api/account/forget')) return {json:async()=>({ok:true})};
    return new Promise(resolve=>{release=resolve;});
  };
  const pending=h.run('forgetAcct("10001")');
  // Wait until the deletion response has been handled and refresh is pending.
  while(!release) await Promise.resolve();
  assert.equal(h.run('ACCOUNT_ID'),null);
  assert.equal(h.run('TOURS.length+MY_TOURS.length+PENDING.size+CONFIRMED.size+ENROLLED.size+MINE.size+PICK_TOURS.size'),0);
  assert.ok(!h.elements.get('myList').innerHTML.includes('old'));
  const response=state();
  response.accounts=response.accounts.slice(1);
  response.active_account_id='10002';
  release({json:async()=>response});
  await pending;
  assert.equal(h.run('ACCOUNT_ID'),'10002');
  assert.deepEqual(loaded,['10002']);
});

test('deleting the last account leaves no actionable tournament data',async()=>{
  const h=harness();
  h.run(`ACCOUNT_ID="10001"; LAST_ACTIVE="10001";
    ACCOUNT_ROWS=new Map([["10001",{version:1}]]); TOURS=[{tournament_id:"old"}];`);
  const response=state(); response.accounts=[]; response.active_account_id=null;
  h.context.fetch=async(url)=>({json:async()=>url.startsWith('/api/account/forget')?{ok:true}:response});
  await h.run('forgetAcct("10001")');
  assert.equal(h.run('ACCOUNT_ID'),null);
  assert.equal(h.run('TOURS.length'),0);
  assert.equal(h.elements.get('btnBatch').disabled,true);
});

test('the floating batch button requires picks on both sides and hides on the subscriptions page',()=>{
  const h=harness();
  const button=h.elements.get('btnBatch');
  const change=(kind,id,checked)=>h.events.change({target:{
    classList:{contains:value=>value==='pick'},dataset:{kind,id},checked
  }});
  h.run('currentPage="events"; updateBatchButton();');
  assert.equal(button.hidden,true);
  change('acct','10001',true);
  assert.equal(button.hidden,true);
  change('acct','10001',false);
  change('tour','test-event',true);
  assert.equal(button.hidden,true);
  change('acct','10001',true);
  assert.equal(button.hidden,false);
  change('tour','test-event',false);
  assert.equal(button.hidden,true);
  change('tour','test-event',true);
  h.run('currentPage="subs"; updateBatchButton();');
  assert.equal(button.hidden,true);
  h.run('currentPage="events"; updateBatchButton();');
  assert.equal(button.hidden,false);
  change('acct','10001',false);
  assert.equal(button.hidden,true);
});

test('batch signup confirms names and count, deduplicates in-flight submission and keeps failed selections',async()=>{
  const h=harness();
  h.run(`currentPage="events"; ACCOUNT_ID="10001";
    ACCOUNT_ROWS=new Map([["10001",{nickname:"test-account",account_id:"10001",version:1}]]);
    TOURS=[{tournament_id:"test-event",name:"test-name"},{tournament_id:"test-failed",name:"test-other"}];
    PICK_ACCTS.add("10001"); PICK_TOURS.add("test-event"); PICK_TOURS.add("test-failed");
    reloadAll=async()=>{};`);
  const successful={checked:true,dataset:{id:'test-event'}};
  const failed={checked:true,dataset:{id:'test-failed'}};
  h.context.document.querySelectorAll=selector=>selector.endsWith(':checked')?[successful,failed]:[];
  const confirmations=[];
  h.context.confirm=message=>{confirmations.push(message);return confirmations.length>1;};
  const calls=[];
  let release;
  h.context.fetch=(url,opts)=>{
    calls.push([url,JSON.parse(opts.body)]);
    return new Promise(resolve=>{release=resolve;});
  };
  await h.run('batchEnroll()');
  assert.equal(calls.length,0);
  assert.match(confirmations[0],/1 个账号 × 2 场赛事 = 2 次提交/);
  assert.match(confirmations[0],/test-account \(UID 10001\) → test-name/);

  const pending=h.run('batchEnroll()');
  await h.run('batchEnroll()');
  h.run('updateBatchButton()');
  const button=h.elements.get('btnBatch');
  assert.equal(button.hidden,false);
  assert.equal(button.disabled,true);
  assert.equal(confirmations.length,2);
  assert.equal(calls.length,1);
  assert.match(calls[0][0],/^\/api\/batch_enroll/);
  assert.deepEqual(calls[0][1],{account_ids:['10001'],tournaments:['test-event','test-failed']});
  release({json:async()=>({ok:true,results:[
    {account_id:'10001',tournament_id:'test-event',result:'ok'},
    {account_id:'10001',tournament_id:'test-failed',result:'fail',reason:'test-failure'}
  ]})});
  await pending;

  assert.match(h.elements.get('batchSummary').textContent,/成功 1.*失败 1/);
  assert.match(h.elements.get('batchDetails').innerHTML,/test-other：失败：test-failure/);
  assert.equal(successful.checked,false);
  assert.equal(failed.checked,true);
  assert.deepEqual(Array.from(h.run('PICK_TOURS')),['test-failed']);
  assert.equal(button.hidden,true);
  h.run('closeBatchResult()');
  assert.equal(button.disabled,false);
});

test('batch signup preserves a late response after switching accounts and releases its button',async()=>{
  const h=harness();
  h.run(`currentPage="events"; ACCOUNT_ID="10001"; PICK_ACCTS.add("10001"); PICK_TOURS.add("test-event");`);
  let release;
  h.context.fetch=()=>new Promise(resolve=>{release=resolve;});
  const pending=h.run('batchEnroll()');
  h.run('updateBatchButton()');
  assert.equal(h.elements.get('btnBatch').disabled,true);
  h.run('ACCOUNT_ID="10002"; ACCOUNT_EPOCH++;');
  release({json:async()=>({ok:true,results:[{account_id:'10001',tournament_id:'test-event',result:'ok'}]})});
  await pending;
  assert.match(h.elements.get('batchDetails').innerHTML,/UID 10001/);
  assert.equal(h.run('batchResultOpen'),true);
  assert.equal(h.run('PICK_TOURS.has("test-event")'),true);
  h.run('closeBatchResult()');
  assert.equal(h.elements.get('btnBatch').disabled,false);
});

for (const results of [['ok','ok'],['skip','skip'],['ok','fail'],['fail','fail'],['skip','fail']]) {
  test(`batch result dialog reports ${results.join('/')} and retains the submission snapshot`,async()=>{
    const h=harness();
    h.run(`currentPage="events"; ACCOUNT_ID="10001";
      ACCOUNT_ROWS=new Map([["10001",{nickname:'test-<b>&"',account_id:"10001"}]]);
      TOURS=[{tournament_id:"one",name:"test-<event>"},{tournament_id:"two",name:"test-two"}];
      PICK_ACCTS.add("10001"); PICK_TOURS.add("one"); PICK_TOURS.add("two"); reloadAll=async()=>{};`);
    let release;
    h.context.fetch=()=>new Promise(resolve=>{release=resolve;});
    const pending=h.run('batchEnroll()');
    h.run('ACCOUNT_ROWS.get("10001").nickname="test-renamed"; TOURS=[];');
    release({json:async()=>({ok:true,results:results.map((result,i)=>({
      account_id:'10001',tournament_id:i?'two':'one',result,reason:'test-<reason>'}))})});
    await pending;
    assert.equal(h.run('batchResultOpen'),true);
    const summary=h.elements.get('batchSummary').textContent;
    for (const [key,label] of [['ok','成功'],['skip','跳过'],['fail','失败']]) {
      assert.ok(summary.includes(`${label} ${results.filter(x=>x===key).length}`));
    }
    assert.match(summary,/待确认 0/);
    const detail=h.elements.get('batchDetails').innerHTML;
    assert.match(detail,/test-&lt;b&gt;&amp;&quot;/);
    assert.match(detail,/test-&lt;event&gt;/);
    assert.doesNotMatch(detail,/test-renamed|<b>|<event>|<reason>/);
    if(results.includes('fail')) assert.match(detail,/test-&lt;reason&gt;/);
    // Polling may rename accounts; navigation may redraw the other panel.
    h.run('renderAccountList(); currentPage="subs"; updateBatchButton(); updateBackToTop();');
    assert.equal(h.run('batchResultOpen'),true);
    assert.equal(h.elements.get('batchDetails').innerHTML,detail);
    assert.equal(h.elements.get('backToTop').hidden,true);
    h.run('closeBatchResult(); renderAccountList(); updateBatchButton();');
    assert.equal(h.run('batchResultOpen'),false);
  });
}

for (const kind of ['network','timeout','missing','duplicate','unknown','invalid','rejected']) {
  test(`batch ${kind} response marks unresolved pairs pending and never resubmits`,async()=>{
    const h=harness();
    h.run(`currentPage="events"; ACCOUNT_ID="10001"; PICK_ACCTS.add("10001");
      PICK_TOURS.add("one"); PICK_TOURS.add("two"); reloadAll=async()=>{};`);
    let calls=0, timeout, signal, release;
    h.context.setTimeout=(fn,ms)=>{if(ms===120000)timeout=fn;return 1;};
    h.context.fetch=(url,opts)=>{
      calls++; signal=opts.signal;
      if(kind==='network') return Promise.reject(new Error('synthetic-network'));
      if(kind==='timeout') return new Promise(resolve=>{release=resolve;});
      const row={account_id:'10001',tournament_id:'one',result:'ok'};
      const results=kind==='missing'?[row]:kind==='duplicate'?[row,row]:kind==='unknown'?
        [{...row,result:'unexpected'}]:null;
      return Promise.resolve({json:async()=>({ok:kind!=='rejected',results})});
    };
    const pending=h.run('batchEnroll()');
    await h.run('batchEnroll()');
    if(kind==='timeout') timeout();
    await pending;
    assert.equal(calls,1);
    assert.equal(h.run('batchRunning'),false);
    assert.match(h.elements.get('batchSummary').textContent,new RegExp(`待确认 ${kind==='missing'?1:2}`));
    assert.match(h.elements.get('batchDetails').innerHTML,/勿直接重复提交/);
    if(kind==='timeout') {
      assert.equal(signal.aborted,true);
      const detail=h.elements.get('batchDetails').innerHTML;
      release({json:async()=>({ok:true,results:[]})});
      await new Promise(resolve=>setImmediate(resolve));
      assert.equal(h.elements.get('batchDetails').innerHTML,detail);
    }
    await h.run('batchEnroll()');
    assert.equal(calls,1); // The result modal blocks further submission.
  });
}

test('an explicit batch rejection preserves the actionable reason and every selection',async()=>{
  const h=harness();
  h.run(`currentPage="events"; ACCOUNT_ID="10001"; PICK_ACCTS.add("10001");
    for(let i=0;i<61;i++) PICK_TOURS.add("test-event-"+i); reloadAll=async()=>{};`);
  let calls=0;
  h.context.fetch=async()=>{
    calls++;
    return {json:async()=>({ok:false,code:'batch_limit_exceeded',
      error:'一次最多 60 次提交（当前 61 次），请减少账号或赛事 <test-reason>'})};
  };
  await h.run('batchEnroll()');
  assert.match(h.elements.get('batchSummary').textContent,/失败 61 · 待确认 0/);
  const detail=h.elements.get('batchDetails').innerHTML;
  assert.match(detail,/请求被拒绝：一次最多 60 次提交（当前 61 次），请减少账号或赛事 &lt;test-reason&gt;/);
  assert.doesNotMatch(detail,/查询官方报名状态|勿直接重复提交|<test-reason>/);
  assert.equal(h.run('PICK_TOURS.size'),61);
  assert.equal(h.run('PICK_ACCTS.size'),1);
  await h.run('batchEnroll()');
  assert.equal(calls,1);
});

for (const error of ['无法连接代理：test-timeout <test-error>', '响应不是 JSON: test-response',
  'HTTP 502: test-gateway', '一次最多 60 次提交（当前 61 次），请减少账号或赛事']) {
  test(`an unclassified batch error remains pending and preserves its reason: ${error}`,async()=>{
    const h=harness();
    h.run(`currentPage="events"; ACCOUNT_ID="10001"; PICK_ACCTS.add("10001");
      PICK_TOURS.add("one"); reloadAll=async()=>{};`);
    let calls=0;
    h.context.fetch=async()=>{calls++;return {json:async()=>({ok:false,error})};};
    await h.run('batchEnroll()');
    assert.match(h.elements.get('batchSummary').textContent,/失败 0 · 待确认 1/);
    const detail=h.elements.get('batchDetails').innerHTML;
    assert.match(detail,/查询官方报名状态，勿直接重复提交/);
    assert.ok(detail.includes(error.replaceAll('<','&lt;').replaceAll('>','&gt;')));
    assert.doesNotMatch(detail,/请求被拒绝|<test-error>/);
    assert.equal(h.run('PICK_TOURS.has("one")'),true);
    assert.equal(calls,1);
  });
}

for (const stale of [true,'test-old-dialog']) {
  test(`a new batch dialog closes in one Back over a stale history marker ${stale}`,()=>{
    const h=harness();
    h.run('currentPage="events";');
    h.context.history.pushState({batchResult:stale},'','/');
    h.run('showBatchResult([{accountLabel:"test-account",tourName:"test-event",result:"ok"}]);');
    const key=h.context.history.state.batchResult;
    assert.notEqual(key,stale);
    h.elements.get('batchClose').onclick();
    assert.equal(h.run('batchResultOpen'),false);
    assert.equal(h.context.history.state.batchResult,stale);
    h.run('showBatchResult([{accountLabel:"test-account",tourName:"test-event",result:"ok"}]);');
    assert.notEqual(h.context.history.state.batchResult,key);
    h.context.history.back();
    assert.equal(h.run('batchResultOpen'),false);
  });
}

test('repeated close actions await one asynchronous history traversal',()=>{
  const h=harness();
  h.run('currentPage="events"; showBatchResult([{accountLabel:"test-account",tourName:"test-event",result:"ok"}]);');
  let backs=0;
  const back=h.context.history.back;
  h.context.history.back=()=>{backs++;};
  h.run('closeBatchResult(); closeBatchResult();');
  assert.equal(backs,1);
  assert.equal(h.run('batchResultOpen'),true);
  back.call(h.context.history);
  assert.equal(h.run('batchResultOpen'),false);
});

for (const method of ['back','button','cancel','escape','close']) {
  test(`batch result ${method} dismissal consumes its history entry and preserves the page`,()=>{
    const h=harness('/subs');
    h.run('currentPage="subs";');
    h.context.history.pushState(null,'','/subs');
    const scroll=[];
    h.context.window.scrollY=345;
    h.context.window.scrollTo=(...args)=>scroll.push(args);
    h.run('showBatchResult([{accountLabel:"test-account",tourName:"test-event",result:"ok"}]);');
    assert.equal(h.context.history.state.batchResult,h.run('batchDialogOrigin.historyKey'));
    const dialog=h.elements.get('batchDialog');
    if(method==='back') h.context.history.back();
    else if(method==='button') h.elements.get('batchClose').onclick();
    else if(method==='close') { dialog.open=false; dialog.events.close(); }
    else {
      let prevented=false;
      dialog.events[method==='escape'?'keydown':'cancel']({key:'Escape',preventDefault(){prevented=true;}});
      assert.equal(prevented,true);
    }
    assert.equal(h.run('batchResultOpen'),false);
    assert.equal(h.context.history.state,null);
    assert.equal(h.run('currentPage'),'subs');
    assert.deepEqual(scroll,[[0,345]]);
    h.run('renderAccountList();');
    assert.equal(h.run('batchResultOpen'),false);
    // The next Back reaches the prior entry without a stale modal entry.
    h.context.history.state={testMarker:true};
    h.context.history.back();
    assert.equal(h.context.history.state,null);
  });
}

test('closing batch results restores page scroll and focus without reopening on refresh',()=>{
  const h=harness();
  const focus=[],scroll=[];
  h.context.focus=focus;
  h.context.window.scrollY=432;
  h.context.window.scrollTo=(...args)=>scroll.push(args);
  h.run(`currentPage="events"; document.body.style.overflow="auto";
    document.activeElement={isConnected:true,focus:opts=>focus.push(opts)};
    showBatchResult([{accountLabel:"test-account",tourName:"test-event",result:"ok"}]);`);
  assert.equal(h.context.document.body.style.overflow,'hidden');
  h.run('clearAccountView("test-refresh");');
  assert.equal(h.run('batchResultOpen'),true);
  h.run('closeBatchResult(); closeBatchResult(); updateBatchButton();');
  assert.deepEqual(scroll,[[0,432]]);
  assert.equal(focus.length,1);
  assert.equal(focus[0].preventScroll,true);
  assert.equal(h.context.document.body.style.overflow,'auto');
  assert.equal(h.run('batchResultOpen'),false);
});

for (const code of ['identity_conflict','storage_unavailable']) {
  test(`${code} clears every account view and reloads the next selected identity`,async()=>{
    const h=harness();
    h.run(`ACCOUNT_ID="10001"; LAST_ACTIVE="10001";
      ACCOUNT_ROWS=new Map([["10001",{version:1}],["10002",{version:1}]]);
      TOURS=[{tournament_id:"old-event",name:"test-old",group:"mine"}];
      MINE.add("old-event"); PICK_TOURS.add("old-event");
      PENDING.set("old-event","enroll"); ENROLLED.set("old-event",{});
      CONFIRMED.set("old-event",{want:true,until:Date.now()+10000});
      renderMyList();`);
    assert.match(h.elements.get('myList').innerHTML,/old-event/);
    let response={ok:false,code};
    h.context.fetch=async()=>({json:async()=>response});
    await h.run('refresh()');
    assert.equal(h.run('ACCOUNT_ID'),null);
    assert.equal(h.run('TOURS.length+MY_TOURS.length+PENDING.size+CONFIRMED.size+ENROLLED.size+MINE.size+PICK_TOURS.size'),0);
    assert.equal(h.elements.get('myList').innerHTML,'');
    const loaded=[]; h.context.loaded=loaded;
    h.run('loadTournaments=async()=>{loaded.push(ACCOUNT_ID);};');
    response=state(); response.accounts=response.accounts.slice(1); response.active_account_id='10002';
    await h.run('refresh()');
    assert.equal(h.run('ACCOUNT_ID'),'10002');
    assert.deepEqual(loaded,['10002']);
    assert.equal(h.elements.get('myList').innerHTML,'');
  });
}

test('manual account selection clears old buttons before its request returns',async()=>{
  const h=harness();
  h.run(`ACCOUNT_ID="10001"; LAST_ACTIVE="10001";
    TOURS=[{tournament_id:"old-event",group:"mine"}]; PICK_TOURS.add("old-event");
    renderMyList();`);
  let release;
  const response=state(); response.active_account_id='10002';
  h.context.fetch=(url)=>url.startsWith('/api/account/select')?
    new Promise(resolve=>{release=resolve;}):Promise.resolve({json:async()=>response});
  const pending=h.run('selectAcct("10002")');
  assert.equal(h.elements.get('myList').innerHTML,'');
  assert.equal(h.run('PICK_TOURS.size'),0);
  release({json:async()=>({ok:true})});
  await pending;
  assert.equal(h.run('ACCOUNT_ID'),'10002');
});
