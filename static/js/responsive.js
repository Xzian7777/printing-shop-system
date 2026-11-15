/* Reflow the existing records without cloning controls or changing their handlers. */
(() => {
    'use strict';
    const menuSelector = '.status-dropdown-menu, .action-menu-dropdown, #notifMenu';
    const notifications = document.getElementById('notifDropdownContainer');
    const notificationHome = document.createComment('Desktop notification position');
    notifications?.before(notificationHome);

    function prepareTable(table) {
        const headers = Array.from(table.querySelectorAll('thead th'));
        if (!headers.length) return;
        table.classList.add('responsive-records');
        headers.forEach(header => header.setAttribute('scope', 'col'));
        table.querySelectorAll('tbody tr').forEach(row => {
            Array.from(row.cells).forEach((cell, index) => {
                if (cell.colSpan > 1) {
                    cell.classList.add('record-message');
                    return;
                }
                cell.dataset.label = headers[index]?.textContent.trim() || 'Details';
            });
        });
        table.parentElement.classList.add('records-scroll');
    }

    function prepareTables(root) {
        if (root.matches?.('table')) prepareTable(root);
        root.querySelectorAll?.('table').forEach(prepareTable);
        const table = root.closest?.('table');
        if (table) prepareTable(table);
    }

    function positionMenu(menu) {
        if (menu.classList.contains('hidden')) {
            if (menu.matches(':popover-open')) menu.hidePopover();
            return;
        }
        const anchor = menu.parentElement.querySelector('button');
        if (!anchor) return;
        const viewport = window.visualViewport;
        const left = viewport?.offsetLeft || 0;
        const top = viewport?.offsetTop || 0;
        const width = viewport?.width || window.innerWidth;
        const height = viewport?.height || window.innerHeight;
        const rect = anchor.getBoundingClientRect();
        if (!menu.classList.contains('viewport-menu')) menu.classList.add('viewport-menu');
        // The top layer keeps menus clear of scrolling tables and filtered sidebars.
        if (typeof menu.showPopover === 'function') {
            menu.setAttribute('popover', 'manual');
            if (!menu.matches(':popover-open')) menu.showPopover();
        }
        menu.style.maxWidth = `${Math.max(0, width - 16)}px`;
        menu.style.maxHeight = `${Math.max(0, height - 16)}px`;
        const bounds = menu.getBoundingClientRect();
        menu.style.left = `${Math.max(left + 8, Math.min(rect.right - bounds.width, left + width - bounds.width - 8))}px`;
        const below = rect.bottom + 6;
        const above = rect.top - bounds.height - 6;
        menu.style.top = `${Math.max(top + 8, Math.min(below + bounds.height <= top + height - 8 ? below : above, top + height - bounds.height - 8))}px`;
    }

    function closeMenus() {
        document.querySelectorAll(menuSelector).forEach(menu => menu.classList.add('hidden'));
    }

    function syncViewport() {
        const viewport = window.visualViewport;
        document.documentElement.style.setProperty('--available-height', `${viewport?.height || window.innerHeight}px`);
        if (notifications) {
            const mobileSlot = document.getElementById('mobileNotificationsSlot');
            if (window.innerWidth < 1024 && mobileSlot && notifications.parentElement !== mobileSlot) {
                mobileSlot.append(notifications);
            } else if (window.innerWidth >= 1024 && notifications.parentNode !== notificationHome.parentNode) {
                notificationHome.after(notifications);
            }
        }
        const drawer = document.getElementById('mobileSidebarDrawer');
        if (window.innerWidth >= 1024 && drawer && !drawer.classList.contains('-translate-x-full')) {
            toggleMobileSidebar();
        }
        // Restore the two-pane inbox after leaving its phone-only detail view.
        if (window.innerWidth >= 768) {
            document.getElementById('adminChatSidebar')?.classList.remove('hidden');
            document.getElementById('adminChatMainPane')?.classList.remove('hidden');
        } else {
            const inbox = document.getElementById('adminChatSidebar');
            const pane = document.getElementById('adminChatMainPane');
            if (inbox && pane && !inbox.classList.contains('hidden') && !pane.classList.contains('hidden')) {
                pane.classList.add('hidden');
            }
        }
        closeMenus();
    }

    prepareTables(document.querySelector('main') || document.body);
    const observer = new MutationObserver(records => {
        records.forEach(record => {
            if (record.type === 'attributes') {
                positionMenu(record.target);
                return;
            }
            record.addedNodes.forEach(node => {
                if (node.nodeType === Node.ELEMENT_NODE) prepareTables(node);
            });
            const menu = record.target?.closest?.(menuSelector);
            if (menu) positionMenu(menu);
        });
        document.querySelectorAll(menuSelector).forEach(menu => {
            observer.observe(menu, {attributes: true, attributeFilter: ['class']});
        });
    });
    observer.observe(document.body, {childList: true, subtree: true});
    document.querySelectorAll(menuSelector).forEach(menu => observer.observe(menu, {attributes: true, attributeFilter: ['class']}));
    window.addEventListener('resize', syncViewport);
    window.visualViewport?.addEventListener('resize', syncViewport);
    document.addEventListener('scroll', event => {
        // Keep an open menu usable while scrolling its own list.
        if (!event.target.closest?.(menuSelector)) closeMenus();
    }, true);
    document.addEventListener('keydown', event => {
        if (event.key !== 'Escape') return;
        closeMenus();
        const drawer = document.getElementById('mobileSidebarDrawer');
        if (drawer && !drawer.classList.contains('-translate-x-full')) toggleMobileSidebar();
    });
    document.getElementById('mobileSidebarDrawer')?.addEventListener('keydown', event => {
        if (event.key !== 'Tab') return;
        const controls = Array.from(event.currentTarget.querySelectorAll('a, button, input'))
            .filter(control => control.getClientRects().length && !control.disabled);
        const first = controls[0];
        const last = controls[controls.length - 1];
        if (event.shiftKey && document.activeElement === first) {
            event.preventDefault(); last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault(); first?.focus();
        }
    });
    syncViewport();
})();
