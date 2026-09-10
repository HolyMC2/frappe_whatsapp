// Actual saved-account Desk source, with no browser or network dependency.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../doctype/whatsapp_account/whatsapp_account.js'), 'utf8');

function fixture() {
    const handlers = [], calls = [], messages = [], buttons = {}, routes = [];
    let dialog, rendered = '', routeClick;
    const result = {account_name: 'saved-account', account_modified: 'revision-one', mode: 'Live', status: 'Active',
        configuration: {token: 'configured'}, check: {state: 'not_checked', source: 'local_configuration'},
        scope: {phone_id: '100', business_id: '200', app_id: '300'},
        phone_receipts: {available: true, total: 3, backlog: 1, failed: 1},
        waba_receipts: {available: true, total: 2, backlog: 0, failed: 0}, outbox: {available: true, unknown: 1, failed: 0, backlog: 0}};
    const frappe = {user_roles: ['System Manager'], utils: {escape_html: value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;')},
        ui: {form: {on: (_, value) => handlers.push(value)}, Dialog: function (config) {
            dialog = config; this.fields_dict = {summary: {$wrapper: {html: value => {rendered = value;}, on: (_, __, fn) => {routeClick = fn;}}}};
            this.show = () => {}; this.hide = () => {};
        }}, msgprint: value => messages.push(value), set_route: (...args) => routes.push(args),
        call: async value => {calls.push(value); if (frappe.beforeReturn) frappe.beforeReturn(); return {message: frappe.result || result};}};
    const frm = {doc: {name: 'saved-account', modified: 'revision-one'}, dirty: false, fresh: false,
        is_new: () => frm.fresh, is_dirty: () => frm.dirty, add_custom_button: (name, fn) => {buttons[name] = fn;}};
    vm.runInNewContext(source, {frappe, __: (value, args = []) => value.replace(/\{(\d+)\}/g, (_, n) => args[n])});
    const refresh = () => handlers.forEach(handler => handler.refresh(frm));
    refresh();
    return {frappe, frm, calls, messages, buttons, routes, result, refresh, dialog: () => dialog,
        html: () => rendered, route: kind => routeClick({currentTarget: {dataset: {healthRoute: kind}}})};
}

(async () => {
    let t = fixture();
    t.frm.dirty = true;
    await t.buttons.Health();
    assert.equal(t.calls.length, 0);
    t = fixture();
    t.frappe.user_roles = [];
    await t.buttons.Health();
    assert.equal(t.calls.length, 0);
    t = fixture();
    await t.buttons.Health();
    assert.equal(t.calls.length, 1);
    assert.equal(t.calls[0].method, 'frappe_whatsapp.account_health.get_health');
    assert.deepEqual(JSON.parse(JSON.stringify(t.calls[0].args)), {account_name: 'saved-account', expected_modified: 'revision-one'});
    assert.ok(t.html().includes('Unknown'));
    await t.dialog().primary_action();
    assert.equal(t.calls[1].method, 'frappe_whatsapp.account_health.check_phone');
    await t.dialog().secondary_action();
    assert.equal(t.calls[2].method, 'frappe_whatsapp.account_health.get_health');

    t = fixture();
    t.result.configuration.token = '<img src=x onerror=bad>';
    t.result.waba_observations = [{event_type: '<script>', received_at: 'today', receipt_state: '<img>', source: 'signed_waba_receipt'}];
    await t.buttons.Health();
    assert.ok(t.html().includes('&lt;script&gt;')); assert.ok(!t.html().includes('<script>'));
    assert.ok(t.html().includes('&lt;img src=x onerror=bad&gt;'));

    for (const change of [t => {t.frm.dirty = true;}, t => {t.frm.doc.name = 'other-account';},
        t => {t.frm.doc.modified = 'revision-two';}, t => {t.frappe.user_roles = [];}]) {
        t = fixture(); await t.buttons.Health(); change(t);
        await t.dialog().primary_action(); await t.dialog().secondary_action(); t.route('phone'); t.route('outbox');
        assert.equal(t.calls.length, 1); assert.equal(t.routes.length, 0);
        assert.equal(t.frappe.route_options, undefined);
    }
    t = fixture();
    t.result.account_name = 'foreign-account';
    await t.buttons.Health();
    assert.equal(t.dialog(), undefined);
    t = fixture();
    t.frappe.beforeReturn = () => {t.frm.doc.modified = 'changed-during-call';};
    await t.buttons.Health();
    assert.equal(t.dialog(), undefined);

    t = fixture(); await t.buttons.Health();
    assert.ok(t.html().includes('Open customer conversations'));
    assert.ok(t.html().includes('Envíos panel'));
    assert.ok(!t.html().includes('Open outbound intents'));
    t.frappe.route_options = {provider: 'Instagram', account_id: 'foreign-account', unrelated: 'stale-option'};
    t.route('phone'); t.route('waba'); t.route('outbox');
    assert.deepEqual(JSON.parse(JSON.stringify(t.routes)), [
        ['List', 'Meta Webhook Receipt', {provider: 'WhatsApp', account_id: '100', app_id: '300'}],
        ['List', 'Meta Webhook Receipt', {provider: 'WhatsApp', account_id: '200', app_id: '300'}],
        ['customer-conversations']
    ]);
    assert.deepEqual(JSON.parse(JSON.stringify(t.frappe.route_options)), {provider: 'WhatsApp', account_id: '100'});
    assert.ok(t.buttons['Subscribe App to Webhooks']); // Existing action preserved.
    process.stdout.write('WhatsApp Account Health: 11 behavior scenarios passed\n');
})().catch(error => {process.stderr.write(error.stack + '\n'); process.exitCode = 1;});
