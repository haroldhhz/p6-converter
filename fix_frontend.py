"""Fix frontend bugs: remove confidence, fix schedule dropdown, fix score display."""

# Fix index.html: remove confidence <th>
with open('/Users/harold/Documents/Work/p6-converter/frontend/index.html', 'r') as f:
    html = f.read()
html = html.replace('<th class="col-confidence">Confidence</th>\n', '')
with open('/Users/harold/Documents/Work/p6-converter/frontend/index.html', 'w') as f:
    f.write(html)
print('index.html: removed confidence th')

# Fix app.js
with open('/Users/harold/Documents/Work/p6-converter/frontend/app.js', 'r') as f:
    js = f.read()

changes = 0

# 1. Remove confidence <td> from risk table rows
old1 = '        <td class="col-confidence">${confidenceHtml(r.confidence)}</td>\n        '
if old1 in js:
    js = js.replace(old1, '        ')
    changes += 1
    print('Removed confidence <td> from risk rows')

# 2. Remove confidence section from evidence panel
old2 = '            <div class="evidence-section"><span class="evidence-label">Confidence</span><span>${Math.round((r.confidence || 0) * 100)}%</span></div>\n          '
if old2 in js:
    js = js.replace(old2, '          ')
    changes += 1
    print('Removed confidence section from panel')

# 3. Add SCHEDULE_OPTIONS and scheduleSelectHtml function
old3 = 'const SCORE_OPTIONS = [1,2,3,4,5,6].map(n => `<option value="${n}">${n} - ${SCORE_LABELS[n]}</option>`).join("");'
if old3 in js and 'SCHEDULE_OPTIONS' not in js:
    sched_opts = (
        'const SCHEDULE_OPTIONS = [1,2,3,4,5,6].map(n => `<option value="${n}">${n} - ${SCORE_LABELS[n]}</option>`).join("");'
        '\n\nfunction scheduleSelectHtml(val, idx) {'
        '\n  return `<select class="score-select score-inline" data-idx="${idx}" data-field="current_schedule"><option value="0">- select -</option>${SCHEDULE_OPTIONS}</select>`'
        '\n    .replace(`value="${val || 0}"`, `value="${val || 0}" selected`);'
        '\n}'
    )
    js = js.replace(old3, old3 + sched_opts)
    changes += 1
    print('Added SCHEDULE_OPTIONS and scheduleSelectHtml')

# 4. Replace schedule impact dropdown in table rows
old4 = '<td class="col-score">${scoreSelectHtml("current_schedule", r.current_schedule, r._idx)}</td>'
new4 = '<td class="col-score">${scheduleSelectHtml(r.current_schedule, r._idx)}</td>'
if old4 in js:
    js = js.replace(old4, new4)
    changes += 1
    print('Replaced schedule impact dropdown in table rows')

# 5. Fix modal schedule dropdown to use SCHEDULE_OPTIONS
old5 = 'data-field="current_schedule"><option value="0">- select -</option>${SCORE_OPTIONS}</select></td>'
new5 = 'data-field="current_schedule"><option value="0">- select -</option>${SCHEDULE_OPTIONS}</select></td>'
if old5 in js:
    js = js.replace(old5, new5)
    changes += 1
    print('Fixed modal schedule dropdown to use SCHEDULE_OPTIONS')

# 6. Fix ratingBadgeHtml to show score even when score is 0
old6 = "function ratingBadgeHtml(label, score) {\n  if (!label || label === '-') return '<span style=\"color:#94a3b8;font-size:0.78rem\">-</span>';"
new6 = "function ratingBadgeHtml(label, score) {\n  if (score === 0 || score === '' || score === undefined) return '<span style=\"color:#94a3b8;font-size:0.78rem\">-</span>';"
if old6 in js:
    js = js.replace(old6, new6)
    changes += 1
    print('Fixed ratingBadgeHtml to handle 0 score')

print('Total changes applied:', changes)
with open('/Users/harold/Documents/Work/p6-converter/frontend/app.js', 'w') as f:
    f.write(js)
