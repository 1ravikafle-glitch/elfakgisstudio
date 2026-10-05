/* ================================================================
   Declarative element actions.
   ------------------------------------------------------------
   The templates used to carry inline handlers — onclick="fn('x')" and
   friends — 86 of them. The Content-Security-Policy sets script-src
   without 'unsafe-inline', because that is the part which actually
   neutralises an injected <script> or an inline handler. So every one of
   those handlers was refused by the browser: the page rendered perfectly,
   looked healthy, and was almost entirely dead. The mode list in the left
   nav was the most visible symptom — clicking A through I changed nothing.

   Rather than weaken the policy to 'unsafe-inline' — which would hand back
   exactly the XSS surface the policy exists to remove — the handlers became
   data attributes and this file dispatches them. One delegated listener per
   event type, no inline script anywhere, policy unchanged.

   The spec string is the original handler text. It is parsed, never eval'd:
   'unsafe-eval' is absent from the policy too, and a parser is a few lines
   where an eval hole would be permanent.
   ================================================================ */
(function (global) {
    'use strict';

    var EVENTS = ['click', 'change', 'input', 'submit'];

    /* Split "a, 'b,c', 3" honouring quotes. Returns trimmed string parts. */
    function splitArgs(src) {
        var out = [], cur = '', quote = null, i;
        for (i = 0; i < src.length; i++) {
            var c = src.charAt(i);
            if (quote) {
                if (c === quote) { out.push(cur); cur = ''; quote = null; continue; }
                if (c === '\\') { cur += src.charAt(i + 1); i++; continue; }
                cur += c;
                continue;
            }
            if (c === '"' || c === "'") { quote = c; continue; }
            if (c === ',') { out.push(cur.trim()); cur = ''; continue; }
            cur += c;
        }
        if (cur !== '' || out.length) out.push(cur.trim());
        return out;
    }

    function literal(tok) {
        if (tok === 'true') return true;
        if (tok === 'false') return false;
        if (tok === 'null') return null;
        if (tok === 'undefined') return undefined;
        if (/^-?\d+(?:\.\d+)?$/.test(tok)) return Number(tok);
        if ((tok.charAt(0) === '"' && tok.charAt(tok.length - 1) === '"') ||
            (tok.charAt(0) === "'" && tok.charAt(tok.length - 1) === "'")) {
            return tok.slice(1, -1);
        }
        return null; // not a literal
    }

    /* Resolve one argument. Covers literals plus the handful of expressions
       the templates actually used: this, this.value, event, and a bare
       global name. Anything else is passed as the string itself rather than
       guessed at. */
    function resolveArg(tok, el, event) {
        var lit = literal(tok);
        if (lit !== null) return lit;
        if (tok === 'this') return el;
        if (tok === 'event' || tok === 'evt') return event;
        if (tok === 'this.value') return el ? el.value : undefined;
        if (tok.indexOf('this.') === 0) {
            if (!el) return undefined;
            var parts = tok.slice(5).split('.');
            var v = el;
            for (var i = 0; i < parts.length; i++) {
                if (v === null || v === undefined) return undefined;
                v = v[parts[i]];
            }
            return v;
        }
        if (/^[A-Za-z_$][\w$]*$/.test(tok) && tok in global) return global[tok];
        return tok;
    }

    function run(el, event) {
        var spec = el.getAttribute('data-' + event.type);
        if (!spec) return;
        spec = spec.trim();

        // Form submits must not navigate; every handler here opts out.
        if (event.type === 'submit') event.preventDefault();

        var m = /^([A-Za-z_$][\w$]*)\s*\(([\s\S]*)\)$/.exec(spec);
        if (!m) {
            // Bare name, e.g. "doLogin".
            if (typeof global[spec] === 'function') global[spec].call(el, event);
            return;
        }
        var fn = global[m[1]];
        if (typeof fn !== 'function') return;
        var inner = m[2].trim();
        var args = inner ? splitArgs(inner).map(function (t) { return resolveArg(t, el, event); }) : [];
        fn.apply(el, args);
    }

    EVENTS.forEach(function (type) {
        global.addEventListener(type, function (event) {
            var el = event.target;
            if (!el || !el.closest) return;
            // submit/change/input do not bubble from the control the handler
            // was written against, so fall back to the element itself.
            var holder = el.closest('[data-' + type + ']');
            if (!holder && el.hasAttribute && el.hasAttribute('data-' + type)) holder = el;
            if (!holder) return;
            if (type !== 'submit') {
                // Only act on the control the author bound, not on a click that
                // merely bubbled up from something inside it.
                if (type === 'click' && el !== holder && !holder.contains(el)) return;
            }
            run(holder, event);
        }, type === 'submit' ? true : false);
    });
})(window);