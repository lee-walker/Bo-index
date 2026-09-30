/**
 * 看板渲染验证
 *
 * 用 jsdom 模拟浏览器环境跑一遍 app.js，
 * 校验：渲染无异常、关键 DOM 被正确填充、无违禁词、配色规范正确。
 *
 * 运行：node verify_dashboard.js
 */

const fs = require('fs');
const path = require('path');

const DOCS = path.resolve(__dirname, '../docs');

let JSDOM;
try {
  ({ JSDOM } = require('jsdom'));
} catch {
  console.error('未安装 jsdom，请先运行：npm install jsdom');
  process.exit(1);
}

const FAILED = [];
function check(name, cond, detail = '') {
  console.log(`  [${cond ? 'PASS' : 'FAIL'}] ${name}${!cond && detail ? ' — ' + detail : ''}`);
  if (!cond) FAILED.push(name);
}

const html = fs.readFileSync(path.join(DOCS, 'index.html'), 'utf8');
const latest = JSON.parse(fs.readFileSync(path.join(DOCS, 'data/latest.json'), 'utf8'));
const history = JSON.parse(fs.readFileSync(path.join(DOCS, 'data/history.json'), 'utf8'));

// 桩：ECharts 在 jsdom 中无法真正渲染 canvas，用记录调用的替身
const chartCalls = [];
const echartsStub = {
  init(el) {
    const id = el && el.id ? el.id : 'unknown';
    return {
      setOption(opt) { chartCalls.push({ id, opt }); },
      resize() { chartCalls.push({ id, resize: true }); },
      _id: id,
    };
  },
};

// 桩：fetch 返回本地 JSON
const fetchStub = (url) => {
  const clean = url.split('?')[0];
  const file = clean.replace('./data/', '');
  const map = { 'latest.json': latest, 'history.json': history };
  const data = map[file];
  if (!data) return Promise.reject(new Error('404 ' + url));
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(data) });
};

const dom = new JSDOM(html, {
  runScripts: 'outside-only',
  pretendToBeVisual: true,
  url: 'http://localhost/',
});

const { window } = dom;
window.echarts = echartsStub;
window.fetch = fetchStub;
window.requestAnimationFrame = (cb) => setTimeout(cb, 0);

// 注入并执行 app.js
const appJs = fs.readFileSync(path.join(DOCS, 'app.js'), 'utf8');
try {
  window.eval(appJs);
} catch (err) {
  console.error('❌ app.js 执行抛错：', err.message);
  process.exit(1);
}

// 等待 Promise 链完成
setTimeout(() => {
  const $ = (id) => window.document.getElementById(id);

  console.log('\n== 1. 渲染无异常 ==');
  check('脚本执行未抛错', true);

  console.log('\n== 2. 状态卡 ==');
  const heroState = $('heroState').textContent;
  const heroClass = $('hero').className;
  check('状态文案已填充', heroState && heroState !== '加载中…', heroState);
  check('状态文案与数据一致', heroState === latest.regimeText, heroState);
  check('应用了正确的状态配色类名',
    heroClass.includes('s-offensive'), heroClass);
  check('未落入 unknown 兜底', !heroClass.includes('s-unknown'), heroClass);
  check('日期已填充', $('heroDate').textContent.includes(latest.tradeDate));

  const channels = $('heroChannels').innerHTML;
  check('VTV 通道已渲染', channels.includes('VTV 通道'));
  check('CGDV 通道已渲染', channels.includes('CGDV 通道'));

  console.log('\n== 3. 标的报价 ==');
  const quotes = $('quotes').innerHTML;
  ['QQQ', 'VTV', 'CGDV', 'KO'].forEach((sym) => {
    check(`${sym} 已渲染且价格正确`,
      quotes.includes(sym) && quotes.includes(latest.quotes[sym].price.toFixed(2)),
      `期望 ${latest.quotes[sym].price.toFixed(2)}`);
  });
  check('涨跌配色遵循红涨绿跌（国内惯例）',
    quotes.includes('class="quote-chg up"') && quotes.includes('class="quote-chg down"'),
    '未同时出现 up 与 down');

  console.log('\n== 4. KO 表盘 ==');
  check('KO 数值已填充', $('koValue').textContent.includes(latest.koAlert.roc20.toFixed(2)),
    $('koValue').textContent);
  const fillWidth = $('koFill').style.width;
  check('进度条宽度已设置', !!fillWidth, fillWidth);
  const w = parseFloat(fillWidth);
  check('负值映射后仍可见（不为 0%）', w > 0 && w < 100,
    `实际 ${fillWidth}，负值应落在左半区`);
  check('状态文案已填充', $('koStatus').textContent.length > 0, $('koStatus').textContent);
  check('未触发时不加 alert 类', !$('koStatus').className.includes('alert'));
  check('KO 数值未使用红绿涨跌色',
    !$('koValue').className.includes('up') && !$('koValue').className.includes('down'));

  console.log('\n== 5. 近 5 日表格 ==');
  const rows = $('trendTable').querySelectorAll('tbody tr');
  check('渲染了 5 行（与数据一致）', rows.length === latest.recentTrend.length,
    `期望 ${latest.recentTrend.length}，实际 ${rows.length}`);
  check('含状态徽章', $('trendTable').innerHTML.includes('badge'));

  console.log('\n== 6. 图表 ==');
  check('两个图表均已初始化', chartCalls.filter((c) => c.opt).length >= 2,
    `实际 ${chartCalls.filter((c) => c.opt).length}`);
  const mainCall = chartCalls.find((c) => c.id === 'mainChart' && c.opt);
  const subCall = chartCalls.find((c) => c.id === 'subChart' && c.opt);
  check('主图有数据序列', !!(mainCall && mainCall.opt.series && mainCall.opt.series.length));
  check('主图含 regime 背景色块',
    !!(mainCall && mainCall.opt.series[0].markArea &&
       mainCall.opt.series[0].markArea.data.length > 0));
  check('副图有两条通道线',
    !!(subCall && subCall.opt.series && subCall.opt.series.length === 2),
    subCall ? `实际 ${subCall.opt.series.length} 条` : '无');
  check('副图含零轴基准线',
    !!(subCall && subCall.opt.series[0].markLine));
  const subData = subCall.opt.series[0].data;
  const subDates = subCall.opt.xAxis.data;
  // 看板初始默认渲染 250 天（1年），但 history.json 可能更长，
  // 因此断言应比对「切片后」的长度一致性，而非全量长度。
  const DEFAULT_DAYS = 250;
  const expected = Math.min(DEFAULT_DAYS, history.dates.length);
  check('副图数据长度与 x 轴一致', subData.length === subDates.length,
    `数据 ${subData.length} vs 轴 ${subDates.length}`);
  check('副图按默认 250 天窗口截取', subData.length === expected,
    `期望 ${expected}，实际 ${subData.length}`);
  const mainData = mainCall.opt.series[0].data;
  check('主图与副图长度一致', mainData.length === subData.length,
    `主图 ${mainData.length} vs 副图 ${subData.length}`);

  console.log('\n== 7. 降级提示 ==');
  if (latest.isDegraded) {
    check('降级时横幅可见', !$('degradedBanner').hidden);
  } else {
    check('正常时横幅隐藏', $('degradedBanner').hidden === true);
  }

  console.log('\n== 8. 免责声明 ==');
  const disc = window.document.querySelector('.disclaimer');
  check('免责声明区块存在', !!disc);
  const items = disc ? disc.querySelectorAll('li').length : 0;
  check('免责声明含 6 条', items === 6, `实际 ${items} 条`);
  check('声明未收集用户信息 / 不构成投资建议',
    disc && disc.textContent.includes('不构成任何投资建议'),
    '缺少核心免责表述');

  console.log('\n== 9. 文案合规扫描 ==');
  const pageText = window.document.body.textContent;
  const forbidden = ['建仓', '加仓', '减仓', '平仓', '清仓', '调仓',
    '买入', '卖出', '抄底', '逃顶', '止盈', '止损', '目标价',
    '建议买', '推荐买', '必涨', '稳赚', '买点', '卖点'];
  const hits = forbidden.filter((wd) => pageText.includes(wd));
  check('页面无违禁词', hits.length === 0, `命中 ${hits.join(', ')}`);

  console.log('\n== 10. 页脚 ==');
  const foot = $('foot').textContent;
  check('页脚显示数据日期', foot.includes(latest.tradeDate), foot);
  check('页脚显示数据源', foot.includes(latest.dataSource), foot);

  console.log('\n' + '='.repeat(60));
  if (FAILED.length) {
    console.log(`❌ ${FAILED.length} 项失败：`);
    FAILED.forEach((f) => console.log('   - ' + f));
    process.exit(1);
  }
  console.log('✅ 看板渲染全部验证通过');
  process.exit(0);
}, 300);
