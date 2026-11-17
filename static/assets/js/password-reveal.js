/* Hold-to-reveal eye button for every password field.
 *
 * Pressing and holding the eye (mouse, touch, or Space/Enter) shows the
 * password; releasing hides it again. Fields are enhanced automatically,
 * including ones added to the page later (e.g. inside modals).
 */
(function () {
    const EYE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>';
    const EYE_OFF = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 3l18 18"/><path d="M10.6 5.1A10.9 10.9 0 0 1 12 5c6.4 0 10 7 10 7a17.6 17.6 0 0 1-3.2 4.1M6.6 6.6A17.4 17.4 0 0 0 2 12s3.6 7 10 7a10.6 10.6 0 0 0 5.4-1.4"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/></svg>';
    const LABEL = 'Hold to show the password';

    const style = document.createElement('style');
    style.textContent = `
        .pw-reveal-wrap { position: relative; display: block; }
        .pw-reveal-wrap > input { padding-right: 2.75rem !important; }
        .pw-reveal-wrap > input.is-invalid { background-position: right 2.75rem center; }
        .pw-reveal-btn {
            position: absolute; top: 50%; right: 0.5rem; transform: translateY(-50%);
            width: 2rem; height: 2rem; padding: 0; border: 0; border-radius: 6px;
            display: flex; align-items: center; justify-content: center;
            background: transparent; cursor: pointer; opacity: 0.55;
            touch-action: none; user-select: none; -webkit-user-select: none; -webkit-touch-callout: none;
        }
        .pw-reveal-btn:hover, .pw-reveal-btn:focus-visible, .pw-reveal-btn.is-revealing { opacity: 1; }
        .pw-reveal-btn svg { width: 1.15rem; height: 1.15rem; pointer-events: none; }
    `;
    document.head.appendChild(style);

    function enhance(input) {
        if (input.dataset.pwReveal) return;
        input.dataset.pwReveal = '1';

        const wrap = document.createElement('span');
        wrap.className = 'pw-reveal-wrap';
        input.parentNode.insertBefore(wrap, input);
        wrap.appendChild(input);

        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'pw-reveal-btn';
        button.title = LABEL;
        button.setAttribute('aria-label', LABEL);
        button.innerHTML = EYE;
        // Match the field's own text colour so the icon suits light and dark forms.
        button.style.color = getComputedStyle(input).color;
        wrap.appendChild(button);

        const show = () => {
            input.type = 'text';
            button.classList.add('is-revealing');
            button.innerHTML = EYE_OFF;
        };
        const hide = () => {
            input.type = 'password';
            button.classList.remove('is-revealing');
            button.innerHTML = EYE;
        };

        button.addEventListener('pointerdown', (event) => {
            event.preventDefault(); // keep focus and the caret in the field
            button.setPointerCapture(event.pointerId);
            show();
        });
        ['pointerup', 'pointercancel', 'lostpointercapture', 'blur'].forEach((name) => {
            button.addEventListener(name, hide);
        });
        button.addEventListener('keydown', (event) => {
            if ((event.key === ' ' || event.key === 'Enter') && !event.repeat) {
                event.preventDefault();
                show();
            }
        });
        button.addEventListener('keyup', (event) => {
            if (event.key === ' ' || event.key === 'Enter') hide();
        });
        button.addEventListener('contextmenu', (event) => event.preventDefault());
        window.addEventListener('blur', hide);
    }

    function enhanceAll(root) {
        if (root.matches && root.matches('input[type="password"]')) enhance(root);
        if (root.querySelectorAll) root.querySelectorAll('input[type="password"]').forEach(enhance);
    }

    function start() {
        enhanceAll(document);
        new MutationObserver((mutations) => {
            mutations.forEach((mutation) => mutation.addedNodes.forEach(enhanceAll));
        }).observe(document.body, { childList: true, subtree: true });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }
})();
