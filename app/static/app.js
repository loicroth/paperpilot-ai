'use strict';
(() => {
  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => [...document.querySelectorAll(selector)];
  let state = null, inFlight = false, dirtySettings = false, connectionFailed = false, toastTimer, lastEvent = null;
  const n = (value) => value !== null && value !== undefined && Number.isFinite(Number(value)) ? Number(value) : null;
  const num = (value, digits = 2) => n(value) === null ? '—' : new Intl.NumberFormat('fr-CH', {minimumFractionDigits: digits, maximumFractionDigits: digits}).format(n(value));
  const money = (value, unit = true) => `${num(value)}${unit && n(value) !== null ? ' USDT' : ''}`;
  const isCFD = (item) => item?.kind === 'cfd' || String(item?.symbol || '').startsWith('CAP:');
  const priceText = (value, item) => `${num(value, isCFD(item) && n(value) !== null && n(value) < 10 ? 5 : 2)} ${item?.currency || (isCFD(item) ? 'USD' : 'USDT')}`;
  const assetName = (value) => ({crypto:'Crypto Spot',shares:'Actions CFD',share:'Actions CFD',equity:'Actions CFD',stock:'Actions CFD',etf:'ETF CFD',indices:'Indices CFD',index:'Indices CFD',forex:'Devises CFD',currencies:'Devises CFD',currency:'Devises CFD',commodities:'Matières premières CFD',commodity:'Matières premières CFD'}[String(value || '').toLowerCase()] || value || 'CFD');
  const signed = (value) => n(value) === null ? '—' : `${n(value) > 0 ? '+' : ''}${num(value)}`;
  const time = (value, short = false) => {
    if (value === null || value === undefined || value === '') return '—';
    const parsed = typeof value === 'number' ? new Date(value < 1e11 ? value * 1000 : value) : new Date(value);
    if (Number.isNaN(parsed.getTime())) return String(value);
    return parsed.toLocaleString('fr-CH', short ? {hour:'2-digit', minute:'2-digit', second:'2-digit'} : {day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'});
  };
  const element = (tag, cls, text) => { const el = document.createElement(tag); if (cls) el.className = cls; if (text !== undefined) el.textContent = text; return el; };
  const set = (id, value) => { const el = $(id); if (el) el.textContent = value; };
  const pnlClass = (el, value) => { el.classList.remove('positive','negative'); if (n(value) !== null && n(value) !== 0) el.classList.add(n(value) > 0 ? 'positive' : 'negative'); };
  const array = (value) => Array.isArray(value) ? value : [];
  const actionTag = (action) => element('span', `action ${String(action || 'WAIT').toLowerCase()}`, action || 'WAIT');
  const proposalLabels = {BUY:'Proposition d’achat',SHORT:'Proposition de vente à découvert',SELL:'Proposition de clôture'};
  const refusalLabels = {BUY:'ACHAT REFUSÉ',SHORT:'VENTE À DÉCOUVERT REFUSÉE',SELL:'CLÔTURE REFUSÉE'};
  function decisionTag(decision) {
    const action = String(decision.action || 'WAIT'), proposal = proposalLabels[action];
    if (!proposal) return actionTag(action);
    return element('span', `action ${decision.rejection ? 'sell' : 'wait'}`, decision.rejection ? refusalLabels[action] : proposal);
  }

  function notify(message, isError = false) { const toast = $('#toast'); toast.textContent = message; toast.className = `toast${isError ? ' error' : ''}`; clearTimeout(toastTimer); toastTimer = setTimeout(() => toast.classList.add('hidden'), 6000); }
  function error(message) { const box = $('#requestError'); box.textContent = message; box.classList.toggle('hidden', !message); }
  function showView(view) {
    if (!['overview','history','settings'].includes(view)) view = 'overview';
    $$('[data-page]').forEach(el => el.classList.toggle('hidden', el.dataset.page !== view));
    $$('.nav').forEach(el => el.classList.toggle('active', el.dataset.view === view));
    set('#pageTitle', {overview:'Vue d’ensemble', history:'Trades & décisions', settings:'Paramètres'}[view]);
    if (location.hash !== `#${view}`) history.replaceState(null, '', `#${view}`);
  }
  function detail(title, summary, data) {
    set('#detailTitle', title); set('#detailSummary', summary || '');
    let content = data;
    if (typeof data === 'string') { try { content = JSON.parse(data); } catch (_) {} }
    set('#detailContent', typeof content === 'string' ? content : JSON.stringify(content || {}, null, 2));
    $('#detailDialog').showModal();
  }
  function detailButton(title, summary, data) { const button = element('button','detail-link','Voir le détail'); button.type = 'button'; button.addEventListener('click', () => detail(title,summary,data)); return button; }
  async function request(path, method = 'GET', body = undefined) {
    const options = {method, headers:{}};
    if (body !== undefined) { options.headers['Content-Type'] = 'application/json'; options.headers['X-CSRF-Token'] = state?.csrf_token || ''; options.body = JSON.stringify(body); }
    const response = await fetch(path, {...options, cache:'no-store'});
    if (!response.ok) {
      let message = `Erreur serveur ${response.status}`;
      try { const data = await response.json(); const info = data.detail || data.message || data.error; message = typeof info === 'string' ? info : JSON.stringify(info || data); } catch (_) {}
      throw new Error(message);
    }
    return response.json();
  }
  async function mutate(path, method, body, message) {
    try { error(''); await request(path,method,body); await refresh(); if (message) notify(message); return true; }
    catch (err) { error(err.message); notify(err.message,true); return false; }
  }
  function renderStats(stats) {
    set('#equity', money(stats.equity)); set('#initialCapital', money(stats.initial_capital));
    set('#cash', money(stats.cash)); set('#realizedPnl', `${signed(stats.realized_pnl)} USDT`); pnlClass($('#realizedPnl'),stats.realized_pnl);
    const pct = n(stats.pnl_pct); set('#pnlBadge', `${pct !== null && pct > 0 ? '+' : ''}${num(pct)} %`); pnlClass($('#pnlBadge'),pct);
    set('#closedTrades', num(stats.closed_trades,0)); set('#winRate', n(stats.closed_trades) ? `${num(stats.win_rate,1)} %` : '—');
    set('#winsLosses', `${num(stats.winners,0)} gagnants · ${num(stats.losers,0)} perdants`);
    set('#unrealizedCaption', `Latent : ${signed(stats.unrealized_pnl)} USDT`);
    const metrics = [
      ['Profit factor', n(stats.profit_factor) === null ? (stats.profit_factor === 'infinite' ? '∞' : '—') : num(stats.profit_factor)],
      ['Drawdown maximal', `${num(stats.max_drawdown_pct)} %`],['Gain moyen net', money(stats.avg_win)],['Perte moyenne nette', money(stats.avg_loss)],
      ['Ratio R réalisé moyen', num(stats.realized_rr)],['Frais cumulés', money(stats.total_fees)],['Gains latents', `${signed(stats.unrealized_pnl)} USDT`],['Conserver BTC · capital', money(stats.btc_benchmark_equity)]
    ];
    if (n(stats.margin_used) !== null) metrics.push(['Marge CFD immobilisée', money(stats.margin_used)]);
    if (n(stats.financing_total) !== null) metrics.push(['Financement CFD simulé cumulé', money(stats.financing_total)]);
    const container = $('#statistics'); container.replaceChildren();
    for (const [label,value] of metrics) { const item = element('div','stat'); item.append(element('small','',label),element('strong','',value)); container.append(item); }
    let by = stats.by_symbol;
    if (by && !Array.isArray(by)) by = Object.entries(by).map(([symbol,row]) => typeof row === 'object' ? {symbol,...row} : {symbol,net_pnl:row});
    smallTable('#symbolPerformance',['Actif','Trades','Résultat net'], array(by).map(row => [row.symbol,num(row.trades ?? row.closed_trades ?? row.count,0),`${signed(row.net_pnl ?? row.pnl ?? row.realized_pnl)} USDT`]));
    let daily = stats.daily;
    if (daily && !Array.isArray(daily)) daily = Object.entries(daily).map(([date,row]) => typeof row === 'object' ? {date,...row} : {date,net_pnl:row});
    smallTable('#dailyPerformance',['Date UTC','Trades','Résultat net'],array(daily).slice(-7).reverse().map(row => [row.date || row.day,num(row.trades ?? row.count,0),`${signed(row.net_pnl ?? row.pnl ?? row.realized_pnl)} USDT`]));
  }
  function smallTable(selector, headings, rows) {
    const target = $(selector); target.replaceChildren();
    if (!rows.length) { target.append(element('p','empty','Les résultats apparaîtront après les premières clôtures.')); return; }
    const table = element('table','compact-table'), head = element('thead'), tr = element('tr'), body = element('tbody');
    headings.forEach(x => tr.append(element('th','',x))); head.append(tr); table.append(head,body);
    rows.forEach(row => { const line = element('tr'); row.forEach(value => line.append(element('td','',value))); body.append(line); }); target.append(table);
  }
  function renderPosition(position) {
    const target = $('#positionContent'), panel = $('.position-panel'); target.replaceChildren(); panel.classList.toggle('has-position',Boolean(position)); set('#positionBadge',position ? `1 / 1 · ${isCFD(position) ? 'CFD' : 'SPOT'}` : '0 / 1');
    if (!position) { target.className = 'empty-position'; target.append(element('div','position-glyph','◎'),element('strong','','Aucune position ouverte'),element('p','','Le robot peut attendre aussi longtemps que nécessaire pour trouver une configuration admissible.')); return; }
    target.className = '';
    const heading = element('div','position-symbol'); const profit = element('span','badge',`${signed(position.unrealized_pnl)} USDT`); pnlClass(profit,position.unrealized_pnl); heading.append(element('strong','',`${position.symbol}${isCFD(position) ? ' · '+(position.direction || 'LONG') : ''}`),profit); target.append(heading);
    const grid = element('div','position-data');
    const values = [['Prix d’entrée',priceText(position.entry,position)],['Prix actuel',priceText(position.current_price,position)],['Quantité',num(position.quantity,8)],['Stop-loss',priceText(position.stop_loss,position)],['Take-profit',priceText(position.take_profit,position)],['Ouverte le',time(position.opened_at)]];
    if (isCFD(position)) values.push(['Sens',position.direction || 'LONG'],['Levier',`${num(position.leverage,1)}×`],['Marge immobilisée',money(position.margin)],['Financement simulé',money(position.financing_accrued)],['Durée maximale CFD','24 h puis première cotation négociable']);
    values.forEach(([label,value]) => { const item = element('div'); item.append(element('small','',label),element('strong','',value)); grid.append(item); }); target.append(grid);
    target.append(element('p','position-bottom',position.entry_reason || 'Protection virtuelle surveillée tant que les données sont disponibles.'));
  }
  function renderMarkets(quotes) {
    const container = $('#markets'); container.replaceChildren();
    if (!quotes.length) { container.append(element('p','empty','En attente des cotations des sources connectées.')); return; }
    for (const quote of quotes) {
      const cfd=isCFD(quote), base=String(quote.symbol).replace(/^CAP:/,'').replace(/USDT$/,''), line=element('div','market'), name=element('div','market-name',quote.label || (cfd?base:`${base} / USDT`)), price=element('div','market-price');
      const old=Date.now()-quote.time_ms>20000;
      name.append(element('small','',cfd ? `CFD démo · ${quote.currency || 'USD'}${quote.market_status && quote.market_status!=='TRADEABLE'?' · '+quote.market_status:''}${old?' · voir heure':''}` : old ? 'Cotation du dernier scan · voir heure' : 'Spot · bid / ask observés'));
      price.append(element('strong','',priceText((n(quote.bid)!==null && n(quote.ask)!==null)?(n(quote.bid)+n(quote.ask))/2:null,quote)),element('small','',`Spread ${num(quote.spread_bps ?? ((quote.ask-quote.bid)/quote.bid*10000),2)} pb · ${time(quote.time_ms,true)}`));
      line.append(element('span',`coin${cfd?' cfd-coin':''}`,cfd?'C':base==='BTC'?'₿':base==='ETH'?'Ξ':base.charAt(0)),name,price); container.append(line);
    }
  }
  function renderProviders(providers, cfdEnabled) {
    const capital=providers?.capital || {}, supported=array(capital.supported), blocked=array(capital.blocked), box=$('#providerOverview'), markets=$('#capitalMarkets');
    box.replaceChildren(); markets.replaceChildren();
    const label=capital.connected?'CFD démo connectés':capital.configured?'CFD configurés · connexion à vérifier':'CFD non connectés';
    set('#capitalTag',capital.connected?'Démo connectée':capital.configured?'À vérifier':'Non connectés');
    const text=capital.error || (capital.connected ? `Source de cotations CFD démo : ${supported.length} contrats validés. ${cfdEnabled ? 'Scanner CFD activé. Seuls les marchés ouverts et admissibles sont analysés lorsque le robot fonctionne.' : 'Scanner CFD désactivé : activez-le dans les paramètres pour analyser les marchés ouverts et admissibles.'}` : capital.configured ? 'Connexion enregistrée. Les contrats ne seront utilisés qu’après validation des cotations et des règles.' : 'Ajoutez une connexion Capital.com démo pour débloquer les cotations CFD.');
    set('#capitalStatus',text);
    box.append(element('strong','',label),element('p','',text),element('small','muted-text','Binance Spot · CFD sur actions / ETF / indices / devises / matières premières selon les contrats validés · futures de bourse non intégrés.'));
    const fx=providers?.fx;
    if (fx?.error) box.append(element('p','rejection',`Conversion USD / USDT indisponible : ${fx.error}`));
    if (supported.length) {
      markets.append(element('strong','','Contrats validés pour le scanner'));
      const list=element('ul','provider-list');
      supported.slice(0,40).forEach(item=>list.append(element('li','',typeof item==='string'?item:`${item.label || item.name || item.symbol || item.epic} · ${assetName(item.asset_class || item.type)}`)));
      markets.append(list);
    }
    if (blocked.length) {
      const expander=element('details','provider-blocked'), summary=element('summary','',`${blocked.length} marché(s) exclu(s)`), list=element('ul','provider-list');
      blocked.slice(0,40).forEach(item=>list.append(element('li','',typeof item==='string'?item:`${item.label || item.name || item.symbol || item.epic || 'Marché'} : ${item.reason || item.error || 'Règles ou données non validées'}`)));
      expander.append(summary,list); markets.append(expander);
    }
  }
  function renderScanner(scan, enabled) {
    scan=scan || {}; const rows=array(scan.rows), selected=array(scan.selected);
    set('#scannerSummary', !enabled ? 'Liste manuelle active. Activez le scanner dans Paramètres pour élargir la recherche.' :
      scan.error ? `Scan indisponible : ${scan.error}` : scan.at ? `${rows.length} marchés examinés · ${selected.length} retenus pour l’IA · ${time(scan.at)}` : 'Scanner activé : attend la prochaine clôture M5 après Démarrer.');
    smallTable('#scannerRows',['Marché','Famille','État','Motif'],rows.map(r=>[r.label || r.symbol,assetName(r.asset_class || (String(r.symbol || '').startsWith('CAP:')?'CFD':'crypto')),selected.includes(r.symbol)?'SÉLECTION IA':r.status,r.reason]));
  }
  function renderDecision(decisions) {
    const decision = decisions[0], target = $('#lastDecision'); target.replaceChildren();
    if (!decision) { target.className='decision-empty'; target.append(actionTag('WAIT'),element('p','','Aucune analyse enregistrée. Démarrez la simulation après avoir configuré l’IA.')); return; }
    target.className=''; const heading = element('div','decision-head'); heading.append(decisionTag(decision),element('strong','',decision.symbol || 'Marchés comparés')); target.append(heading,element('p','decision-reason',decision.reason || '—'));
    if (decision.rejection) target.append(element('p','rejection',`Motif du refus : ${decision.rejection}`));
    target.append(detailButton(`${proposalLabels[decision.action] || 'Décision '+decision.action} · ${decision.symbol}`,time(decision.at),decision.detail || decision));
  }
  function renderTables(trades,decisions) {
    const tradeBody = $('#tradesBody'); tradeBody.replaceChildren();
    if (!trades.length) { const row = element('tr'),cell = element('td','empty','Aucun trade clôturé. Aucun historique artificiel n’est généré.'); cell.colSpan=8; row.append(cell); tradeBody.append(row); }
    trades.forEach(trade => {
      const row = element('tr'); [time(trade.opened_at),`${trade.symbol}${isCFD(trade)?' · '+(trade.direction || 'LONG'):''}`,priceText(trade.entry,trade),priceText(trade.exit,trade),num(trade.quantity,8),money(trade.fees)].forEach(x => row.append(element('td','',x)));
      const pnl = element('td','',signed(trade.net_pnl)); pnlClass(pnl,trade.net_pnl); row.append(pnl); const detailCell = element('td'); detailCell.append(detailButton(`Trade ${trade.symbol}`,`${time(trade.opened_at)} → ${time(trade.closed_at)} · ${trade.exit_reason || ''}`,trade.detail || trade)); row.append(detailCell); tradeBody.append(row);
    });
    const decisionBody = $('#decisionsBody'); decisionBody.replaceChildren();
    if (!decisions.length) { const row = element('tr'),cell = element('td','empty','Aucune décision enregistrée.'); cell.colSpan=6; row.append(cell); decisionBody.append(row); }
    decisions.forEach(decision => { const row = element('tr'); row.append(element('td','',time(decision.at)),element('td','',decision.symbol || '—')); const actionCell = element('td'); actionCell.append(decisionTag(decision)); row.append(actionCell,element('td','table-reason',decision.reason || '—'),element('td','table-reason',decision.rejection || (proposalLabels[decision.action] ? 'Aucun refus enregistré' : '—'))); const detailCell = element('td'); detailCell.append(detailButton('Détail de la décision',`${time(decision.at)} · ${decision.symbol || ''}`,decision.detail || decision)); row.append(detailCell); decisionBody.append(row); });
  }
  function renderEvents(events) {
    const container = $('#events'); container.replaceChildren();
    if (!events.length) { container.append(element('p','empty','Aucun événement pour le moment.')); return; }
    events.slice(0,40).forEach(event => { const line = element('div',`event ${event.level || ''}`), date = element('time','',time(event.at)); line.append(date,element('p','',event.message)); container.append(line); });
    const newest = `${events[0].at}:${events[0].message}`;
    if (lastEvent !== null && newest !== lastEvent) { const ev = events[0]; if (/achat|vente|stop|erreur|error|clôtur/i.test(ev.message || '')) notify(ev.message,/error|erreur|critical/i.test(ev.level || '')); }
    lastEvent = newest;
  }
  function renderSettings(settings,usage) {
    if (!dirtySettings) {
      const form = $('#settingsForm');
      for (const [key,value] of Object.entries(settings || {})) { const input = form.elements.namedItem(key); if (input) input.value = key === 'symbols' ? array(value).join(', ') : value; }
      if (n(settings.fee_rate) !== null) form.elements.fee_percent.value = Number((settings.fee_rate * 100).toFixed(5));
    }
    set('#usageCalls',`${num(usage.calls,0)} / ${num(usage.max_calls ?? settings.max_daily_calls,0)} appels`);
    set('#usageTokens',`${num(usage.tokens,0)} / ${num(usage.max_tokens ?? settings.max_daily_tokens,0)} tokens`);
  }
  function renderChart(series) {
    const points = series.filter(point => n(point.equity) !== null); const target = $('#equityChart'); target.replaceChildren();
    if (points.length < 2) { target.append(element('p','empty',points.length ? 'Premier relevé enregistré. La courbe se dessinera au fil du marché.' : 'La courbe apparaîtra après les premiers relevés du marché.')); return; }
    const width=720,height=240,left=58,right=16,top=12,bottom=32,availableW=width-left-right,availableH=height-top-bottom;
    const values = points.flatMap(point => [n(point.equity),n(point.btc_equity)]).filter(value => value !== null);
    let lo=Math.min(...values),hi=Math.max(...values); const margin=Math.max((hi-lo)*.2,1); lo-=margin;hi+=margin;
    const svgNS='http://www.w3.org/2000/svg', svg=document.createElementNS(svgNS,'svg'); svg.setAttribute('viewBox',`0 0 ${width} ${height}`); svg.setAttribute('role','img'); svg.setAttribute('aria-label','Évolution du capital réel de simulation et comparaison avec conserver BTC');
    const add = (tag,attrs,text) => { const node=document.createElementNS(svgNS,tag); Object.entries(attrs).forEach(([key,value])=>node.setAttribute(key,value)); if(text!==undefined)node.textContent=text;svg.append(node);return node; };
    const y = value => top + (hi-value)/(hi-lo)*availableH, x = index => left+index/(points.length-1)*availableW;
    for(let i=0;i<4;i++){const value=lo+(hi-lo)*i/3,Y=y(value);add('line',{x1:left,y1:Y,x2:width-right,y2:Y,stroke:'#28394b','stroke-dasharray':'3 5','stroke-width':'.7'});add('text',{x:left-10,y:Y+3,fill:'#6f869e','font-size':'10','text-anchor':'end'},num(value,1));}
    [0,Math.floor((points.length-1)/2),points.length-1].forEach((index,i)=>add('text',{x:x(index),y:height-9,fill:'#6f869e','font-size':'9','text-anchor':i===0?'start':i===2?'end':'middle'},time(points[index].at)));
    ['btc_equity','equity'].forEach((key,index)=>{let path='',active=false;points.forEach((point,i)=>{if(n(point[key])===null){active=false;return;}path+=`${active?'L':'M'}${x(i).toFixed(2)},${y(n(point[key])).toFixed(2)} `;active=true;});if(path)add('path',{d:path,fill:'none',stroke:index?'#3ad4b2':'#f5ac62','stroke-width':index?'2.5':'1.6','stroke-linejoin':'round','stroke-linecap':'round',opacity:index?'1':'.75'});});
    add('circle',{cx:x(points.length-1),cy:y(n(points.at(-1).equity)),r:3.5,fill:'#3ad4b2',stroke:'#133c35','stroke-width':3}); target.append(svg);
  }
  function render(data) {
    state=data; const health=data.health || {}, control=data.control || {}, stats=data.stats || {};
    $('#configurationNotice').classList.toggle('hidden',Boolean(data.ai_configured)); set('#aiTag',data.ai_configured?'Clé présente':'Non configurée');
    const status=health.status || 'PAUSE', unhealthy=/ERREUR|ERROR|INDISPONIBLE|NON CONFIGUR|INCONFIGUR/i.test(status) || health.market_ok === false || health.ai_ok === false;
    set('#robotStatus',status); set('#robotMessage',health.message || (control.mode==='running'?'Le robot observe les marchés.':'Les nouvelles analyses sont arrêtées.'));
    $('#robotDot').className=`dot ${unhealthy?'error':control.mode==='running'?'':'warn'}`;
    set('#connectionText','Serveur connecté'); $('#connectionDot').className='dot';
    set('#lastQuote',health.last_quote_at ? `Reçu à ${time(health.last_quote_at,true)}`:'Aucun cours reçu');
    set('#lastAnalysis',health.last_analysis_at ? time(health.last_analysis_at):'Aucune analyse');
    $('#startButton').disabled=control.mode==='running'; $('#pauseButton').disabled=control.mode==='paused' || control.mode==='stopped'; $('#stopButton').disabled=control.mode==='stopped';
    renderProviders(data.providers, data.settings?.cfd_enabled === true);
    renderScanner(data.scanner,data.settings?.auto_scanner || data.settings?.cfd_enabled);
    renderStats(stats);renderPosition(data.position);renderMarkets(array(data.quotes));renderDecision(array(data.decisions));renderTables(array(data.trades),array(data.decisions));renderEvents(array(data.events));renderSettings(data.settings || {},data.usage || {});renderChart(array(data.equity));
  }
  async function refresh() {
    if(inFlight)return;inFlight=true;
    try { const data=await request('/api/state');if(connectionFailed){error('');connectionFailed=false;}render(data); }
    catch(err){connectionFailed=true;set('#connectionText','Serveur indisponible');$('#connectionDot').className='dot error';set('#robotStatus','DONNÉES INDISPONIBLES');set('#robotMessage','La page ne reçoit plus l’état du serveur. Vérifiez que le programme fonctionne.');$('#robotDot').className='dot error';error(`Connexion interrompue : ${err.message}. Les valeurs affichées ne sont plus à jour.`);}
    finally{inFlight=false;}
  }
  $$('.nav').forEach(button=>button.addEventListener('click',()=>showView(button.dataset.view)));
  $$('[data-go]').forEach(button=>button.addEventListener('click',()=>showView(button.dataset.go)));
  $$('[data-close]').forEach(button=>button.addEventListener('click',()=>$('#'+button.dataset.close).close()));
  window.addEventListener('hashchange',()=>showView(location.hash.slice(1)));
  $('#startButton').addEventListener('click',()=>mutate('/api/control','POST',{action:'start'},'Démarrage demandé. Consultez le statut et la dernière analyse.'));
  $('#pauseButton').addEventListener('click',()=>mutate('/api/control','POST',{action:'pause'},'Nouvelles entrées en pause. Les protections virtuelles restent surveillées.'));
  $('#stopButton').addEventListener('click',()=>mutate('/api/control','POST',{action:'stop'},'Analyses arrêtées. La protection des positions existantes continue.'));
  $('#settingsForm').addEventListener('input',()=>{dirtySettings=true;set('#settingsSaved','Modifications non enregistrées');});
  $('#settingsForm').addEventListener('submit',async(event)=>{
    event.preventDefault();const form=event.target;if(!form.reportValidity())return;
    const body={};for(const key of ['risk_pct','daily_loss_pct','min_rr','slippage_bps','max_spread_bps','max_atr_pct','max_daily_calls','max_daily_tokens','scanner_limit','ai_shortlist','cfd_leverage','cfd_financing_daily','cfd_scan_limit'])body[key]=Number(form.elements.namedItem(key).value);
    body.auto_scanner=form.elements.auto_scanner.value==='true';
    body.cfd_enabled=form.elements.cfd_enabled.value==='true';
    body.allow_short=form.elements.allow_short.value==='true';
    body.symbols=[...new Set(form.elements.symbols.value.toUpperCase().split(/[,;\s]+/).map(x=>x.replace('/','')).filter(Boolean))];body.model=form.elements.model.value.trim();body.fee_rate=Number(form.elements.fee_percent.value)/100;
    const ok=await mutate('/api/settings','PUT',body,'Paramètres enregistrés.');if(ok){dirtySettings=false;set('#settingsSaved','Enregistré');await refresh();}
  });
  $('#capitalForm').addEventListener('submit',async(event)=>{
    event.preventDefault(); const form=event.target, button=$('#capitalConnect');
    if(!form.reportValidity() || button.disabled)return;
    button.disabled=true;set('#capitalStatus','Vérification de la connexion démo…');
    const body={identifier:form.elements.identifier.value.trim(),api_key:form.elements.api_key.value.trim(),api_password:form.elements.api_password.value};
    try {
      await request('/api/providers/capital/setup','POST',body);
      form.reset(); await refresh(); notify('Connexion démo enregistrée. Activez le scanner CFD dans les paramètres.');
    } catch (_) {
      // Credential setup errors must never echo an API response that could contain a secret.
      set('#capitalStatus','Connexion démo impossible. Vérifiez votre e-mail, votre clé et le mot de passe API personnalisé, puis réessayez.');
      notify('La connexion Capital.com démo n’a pas pu être vérifiée.',true);
    } finally {
      form.elements.api_key.value=''; form.elements.api_password.value='';
      body.api_key='';body.api_password='';button.disabled=false;
    }
  });
  $('#resetButton').addEventListener('click',()=>{$('#resetConfirmation').value='';$('#resetDialog').showModal();$('#resetConfirmation').focus();});
  $('#confirmReset').addEventListener('click',async()=>{if($('#resetConfirmation').value!=='REINITIALISER'){notify('Saisissez exactement REINITIALISER pour confirmer.',true);return;}if(await mutate('/api/reset','POST',{confirmation:'REINITIALISER'},'Portefeuille réinitialisé à 900 USDT virtuels.'))$('#resetDialog').close();});
  showView(location.hash.slice(1));refresh();setInterval(refresh,5000);
})();
