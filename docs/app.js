/* Bo 配对动能指标 — 看板渲染逻辑
 *
 * 数据来源：./data/latest.json 与 ./data/history.json
 * 不做任何指标计算，只做渲染（计算全部在 GitHub Actions 完成）。
 */

(function () {
  'use strict';

  // 状态语义色：进攻=蓝 / 防守=橙 / 分歧=灰（与 style.css 保持一致）
  var REGIME_STYLE = {
    OFFENSIVE: { cls: 's-offensive', badge: 'offensive', color: '#165DFF' },
    DEFENSIVE: { cls: 's-defensive', badge: 'defensive', color: '#FF7D00' },
    NEUTRAL:   { cls: 's-neutral',   badge: 'neutral',   color: '#86909C' }
  };

  var KO_THRESHOLD = 7.0;

  var HISTORY = null;
  var CHARTS = {};

  // ---------------------------------------------------------------------
  // 工具
  // ---------------------------------------------------------------------

  /* GitHub Pages 有约 10 分钟 CDN 缓存，加 10 分钟粒度时间戳绕开陈旧数据 */
  function cacheBust() {
    return '?v=' + Math.floor(Date.now() / 600000);
  }

  function $(id) { return document.getElementById(id); }

  function fmt(v, digits, suffix) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    var s = Number(v).toFixed(digits === undefined ? 2 : digits);
    if (Number(v) > 0) s = '+' + s;
    return s + (suffix || '');
  }

  function pct(v) { return fmt(v, 2, '%'); }

  /* 标的单日涨跌幅：此处是真正的涨跌，用国内惯例红涨绿跌 */
  function chgClass(v) {
    if (v === null || v === undefined || isNaN(v)) return 'flat';
    if (v > 0) return 'up';
    if (v < 0) return 'down';
    return 'flat';
  }

  // ---------------------------------------------------------------------
  // 渲染：状态卡
  // ---------------------------------------------------------------------

  function renderHero(latest) {
    var style = REGIME_STYLE[latest.regime] || { cls: 's-unknown' };
    var hero = $('hero');
    hero.className = 'card hero ' + style.cls;

    $('heroState').textContent = latest.regimeText || '未知状态';
    $('heroDate').textContent = latest.tradeDate + ' 收盘';

    var ind = latest.indicators;
    $('heroChannels').innerHTML =
      channelHtml('VTV 通道', ind.vtvPair.signal) +
      channelHtml('CGDV 通道', ind.cgdvPair.signal);

    // 穿越事件提示（中性化措辞，不用买卖点）
    if (latest.crossoverEvent && latest.crossoverEvent !== 'NONE') {
      var label = latest.crossoverEvent === 'CROSS_DOWN' ? '下穿零轴' : '上穿零轴';
      var tip = document.createElement('div');
      tip.className = 'hero-date';
      tip.style.marginTop = '12px';
      tip.style.fontWeight = '600';
      tip.textContent = '⚠️ 本交易日 VTV 通道' + label;
      $('heroChannels').appendChild(tip);
    }
  }

  function channelHtml(name, val) {
    return '<div class="channel">' +
             '<div class="channel-name">' + name + '</div>' +
             '<div class="channel-val">' + fmt(val, 2) + '</div>' +
           '</div>';
  }

  // ---------------------------------------------------------------------
  // 渲染：标的报价
  // ---------------------------------------------------------------------

  function renderQuotes(latest) {
    var order = ['QQQ', 'VTV', 'CGDV', 'KO'];
    var html = '';
    order.forEach(function (sym) {
      var q = latest.quotes[sym];
      if (!q) return;
      html +=
        '<div class="quote">' +
          '<span class="quote-sym">' + sym + '</span>' +
          '<span class="quote-right">' +
            '<span class="quote-price">' + q.price.toFixed(2) + '</span><br>' +
            '<span class="quote-chg ' + chgClass(q.changePct) + '">' +
              pct(q.changePct) + '</span>' +
          '</span>' +
        '</div>';
    });
    $('quotes').innerHTML = html;
  }

  // ---------------------------------------------------------------------
  // 渲染：KO 表盘
  // ---------------------------------------------------------------------

  function renderKO(latest) {
    var ko = latest.koAlert;
    var val = ko.roc20;

    $('koValue').textContent = pct(val);
    /* KO 涨幅是「避险指标读数」而非股价涨跌，因此不用红绿表意，
       仅在触发阈值时用警戒橙，与进攻/防守的配色体系保持一致。 */
    $('koValue').style.color = ko.isTriggered ? 'var(--c-defensive)' : '';

    /* 进度条映射：负值区间（-7%~0%）占左半，0%~阈值占右半。
       这样负值也有可见长度，避免读数与长度完全脱节。 */
    var clamped = Math.max(-KO_THRESHOLD, Math.min(KO_THRESHOLD, val || 0));
    var ratio = (clamped + KO_THRESHOLD) / (KO_THRESHOLD * 2);
    var fill = $('koFill');
    fill.style.width = (ratio * 100) + '%';
    fill.className = 'gauge-fill' + (ko.isTriggered ? ' alert' : '');

    /* 阈值刻度位置对应 +7% 在映射轴上的位置（即最右端） */
    $('koThreshold').style.left = '100%';

    if (ko.isTriggered) {
      $('koStatus').textContent = '已进入极端避险阈值区间';
    } else {
      var gap = KO_THRESHOLD - (val || 0);
      $('koStatus').textContent =
        gap > 0 ? '未触发（距阈值 ' + gap.toFixed(2) + ' 个百分点）'
                : '未触发';
    }
    $('koStatus').className = 'ko-status' + (ko.isTriggered ? ' alert' : '');
  }

  // ---------------------------------------------------------------------
  // 渲染：近 5 日表格
  // ---------------------------------------------------------------------

  function renderTrend(latest) {
    var tbody = $('trendTable').querySelector('tbody');
    tbody.innerHTML = (latest.recentTrend || []).map(function (r) {
      var st = REGIME_STYLE[r.state] || REGIME_STYLE.NEUTRAL;
      var text = r.state === 'OFFENSIVE' ? '进攻'
               : r.state === 'DEFENSIVE' ? '防守' : '分歧';
      return '<tr>' +
        '<td>' + r.date.slice(5) + '</td>' +
        '<td>' + fmt(r.vtvSignal, 2) + '</td>' +
        '<td>' + fmt(r.cgdvSignal, 2) + '</td>' +
        '<td><span class="badge ' + st.badge + '">' + text + '</span></td>' +
      '</tr>';
    }).join('');
  }

  // ---------------------------------------------------------------------
  // 渲染：图表
  // ---------------------------------------------------------------------

  function sliceHistory(days) {
    if (!HISTORY) return null;
    var n = HISTORY.dates.length;
    var start = (days && days > 0) ? Math.max(0, n - days) : 0;
    return {
      dates:      HISTORY.dates.slice(start),
      qqqClose:   HISTORY.qqqClose.slice(start),
      vtvSignal:  HISTORY.vtvSignal.slice(start),
      cgdvSignal: HISTORY.cgdvSignal.slice(start),
      regimes:    HISTORY.regimes.slice(start),
      events:     (HISTORY.events || []).filter(function (e) {
        return HISTORY.dates.indexOf(e.date) >= start;
      })
    };
  }

  /* 把 regime 序列转成 ECharts markArea 的背景色块 */
  function buildRegimeAreas(h) {
    var areas = [];
    var cur = null;
    for (var i = 0; i < h.regimes.length; i++) {
      var r = h.regimes[i];
      if (!cur || cur.state !== r) {
        if (cur) { cur.end = h.dates[i - 1]; areas.push(cur); }
        cur = { state: r, start: h.dates[i] };
      }
    }
    if (cur) { cur.end = h.dates[h.dates.length - 1]; areas.push(cur); }

    return areas.map(function (a) {
      var color = a.state === 'OFFENSIVE' ? 'rgba(22,93,255,.10)'
                : a.state === 'DEFENSIVE' ? 'rgba(255,125,0,.10)'
                : 'rgba(134,144,156,.08)';
      return [{ xAxis: a.start, itemStyle: { color: color } },
              { xAxis: a.end }];
    });
  }

  function renderCharts(days) {
    var h = sliceHistory(days);
    if (!h || !h.dates.length) return;

    /* ---- 主图：QQQ 收盘价 ---- */
    var main = CHARTS.main;
    main.setOption({
      animation: false,
      grid: { left: 48, right: 16, top: 16, bottom: 24 },
      tooltip: {
        trigger: 'axis',
        confine: true,
        axisPointer: { type: 'line' },
        valueFormatter: function (v) {
          return v === null ? '—' : Number(v).toFixed(2);
        }
      },
      xAxis: {
        type: 'category',
        data: h.dates,
        boundaryGap: false,
        axisLabel: {
          fontSize: 10, color: '#86909C',
          formatter: function (v) { return v.slice(2, 7); }
        },
        axisLine: { lineStyle: { color: '#E5E6EB' } }
      },
      yAxis: {
        type: 'value',
        scale: true,
        axisLabel: { fontSize: 10, color: '#86909C' },
        splitLine: { lineStyle: { color: '#F2F3F5' } }
      },
      series: [{
        name: 'QQQ',
        type: 'line',
        data: h.qqqClose,
        showSymbol: false,
        lineStyle: { width: 1.6, color: '#1D2129' },
        areaStyle: {
          color: {
            type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: 'rgba(29,33,41,.10)' },
              { offset: 1, color: 'rgba(29,33,41,0)' }
            ]
          }
        },
        markArea: { silent: true, data: buildRegimeAreas(h) },
        markPoint: {
          symbolSize: 6,
          label: { show: false },
          data: h.events.map(function (e) {
            var i = h.dates.indexOf(e.date);
            if (i < 0) return null;
            var isCross = e.type === 'CROSS_DOWN' || e.type === 'CROSS_UP';
            return {
              coord: [i, h.qqqClose[i]],
              symbolSize: isCross ? 8 : 6,
              itemStyle: {
                color: e.type === 'KO_ALERT' ? '#FF7D00'
                     : e.type === 'CROSS_DOWN' ? '#165DFF' : '#86909C'
              }
            };
          }).filter(Boolean)
        }
      }]
    }, true);

    /* ---- 副图：双通道信号 + 零轴 ---- */
    var sub = CHARTS.sub;
    sub.setOption({
      animation: false,
      grid: { left: 48, right: 16, top: 16, bottom: 24 },
      legend: {
        data: ['VTV 信号', 'CGDV 信号'],
        right: 8, top: 0,
        itemWidth: 14, itemHeight: 2,
        textStyle: { fontSize: 11, color: '#4E5969' }
      },
      tooltip: {
        trigger: 'axis',
        confine: true,
        valueFormatter: function (v) {
          return v === null ? '—' : (Number(v) > 0 ? '+' : '') + Number(v).toFixed(2);
        }
      },
      xAxis: {
        type: 'category',
        data: h.dates,
        boundaryGap: false,
        axisLabel: {
          fontSize: 10, color: '#86909C',
          formatter: function (v) { return v.slice(2, 7); }
        },
        axisLine: { lineStyle: { color: '#E5E6EB' } }
      },
      yAxis: {
        type: 'value',
        scale: true,
        axisLabel: { fontSize: 10, color: '#86909C' },
        splitLine: { lineStyle: { color: '#F2F3F5' } }
      },
      series: [
        {
          name: 'VTV 信号',
          type: 'line',
          data: h.vtvSignal,
          showSymbol: false,
          connectNulls: true,
          lineStyle: { width: 1.6, color: '#165DFF' },
          markLine: {
            silent: true,
            symbol: 'none',
            label: { show: false },
            lineStyle: { type: 'dashed', color: '#C9CDD4', width: 1 },
            data: [{ yAxis: 0 }]      // 零轴基准
          }
        },
        {
          name: 'CGDV 信号',
          type: 'line',
          data: h.cgdvSignal,
          showSymbol: false,
          connectNulls: true,
          lineStyle: { width: 1.6, color: '#FF7D00' }
        }
      ]
    }, true);
  }

  // ---------------------------------------------------------------------
  // 启动
  // ---------------------------------------------------------------------

  function bindTabs() {
    var tabs = $('rangeTabs');
    tabs.addEventListener('click', function (e) {
      var btn = e.target.closest('button');
      if (!btn) return;
      [].forEach.call(tabs.querySelectorAll('button'), function (b) {
        b.classList.toggle('active', b === btn);
      });
      renderCharts(parseInt(btn.dataset.days, 10));
    });
  }

  function resizeCharts() {
    Object.keys(CHARTS).forEach(function (k) { CHARTS[k].resize(); });
  }

  function fail(err) {
    console.error(err);
    $('heroState').textContent = '数据加载失败';
    $('hero').className = 'card hero s-unknown';
    $('foot').textContent = '数据加载失败，请稍后重试';
  }

  function boot() {
    var bust = cacheBust();

    Promise.all([
      fetch('./data/latest.json' + bust).then(function (r) {
        if (!r.ok) throw new Error('latest.json HTTP ' + r.status);
        return r.json();
      }),
      fetch('./data/history.json' + bust).then(function (r) {
        if (!r.ok) throw new Error('history.json HTTP ' + r.status);
        return r.json();
      })
    ]).then(function (res) {
      var latest = res[0];
      HISTORY = res[1];

      $('degradedBanner').hidden = !latest.isDegraded;

      renderHero(latest);
      renderQuotes(latest);
      renderKO(latest);
      renderTrend(latest);

      CHARTS.main = echarts.init($('mainChart'));
      CHARTS.sub = echarts.init($('subChart'));
      renderCharts(250);

      bindTabs();
      window.addEventListener('resize', resizeCharts);

      $('foot').textContent =
        '数据日期 ' + latest.tradeDate +
        '　·　数据源 ' + latest.dataSource +
        '　·　更新于 ' + (latest.generatedAt || '').replace('T', ' ').replace('Z', ' UTC');
    }).catch(fail);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
