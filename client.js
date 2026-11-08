/**
 * dsh-camera-watermark — Client half.
 *
 * Adds a small "相机水印" action to the Web sidebar footer. Clicking it asks
 * the Host half to start (or reuse) the Flask studio, then opens the returned
 * local URL in a new tab. The Host route is `/camera-watermark/studio`.
 *
 * No DSH Client package is imported: the button is plain React from the
 * browser module table plus `--dsw-alias-*` theme tokens.
 */

window.__ModuleLoader__.load({
  id: 'dsh-camera-watermark',
  factory(require) {
    const React = require('react')
    const h = React.createElement

    const CSS = `
.cw-studio-entry {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: 32px;
  max-width: 100%;
  padding: 0 8px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--dsw-alias-label-secondary, #6b7280);
  font: inherit;
  font-size: 12px;
  line-height: 1;
  cursor: pointer;
  -webkit-tap-highlight-color: transparent;
}
.cw-studio-entry:hover {
  background: var(--dsw-alias-interactive-bg-hover, rgba(0, 0, 0, 0.06));
  color: var(--dsw-alias-label-primary, #111827);
}
.cw-studio-entry:focus-visible {
  outline: 2px solid var(--dsw-alias-state-business-primary, #2f6fed);
  outline-offset: 1px;
}
.cw-studio-entry[data-busy="true"] {
  opacity: 0.6;
  cursor: progress;
}
.cw-studio-entry > span {
  min-width: 0;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
`

    function CameraIcon({ size }) {
      return h('svg', {
        width: size,
        height: size,
        viewBox: '0 0 24 24',
        fill: 'none',
        'aria-hidden': 'true',
        focusable: 'false',
      },
      h('path', {
        d: 'M4 7.5h3.1l1.4-2h6.9l1.4 2H20a2 2 0 0 1 2 2v8.5a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V9.5a2 2 0 0 1 2-2Z',
        stroke: 'currentColor',
        strokeWidth: 1.6,
        strokeLinejoin: 'round',
      }),
      h('circle', {
        cx: 12,
        cy: 13.2,
        r: 3.4,
        stroke: 'currentColor',
        strokeWidth: 1.6,
      }),
      h('circle', { cx: 18.2, cy: 10.3, r: 1, fill: 'currentColor' }))
    }

    function useChinese() {
      try {
        return /^zh/i.test(navigator.language || '')
      } catch {
        return true
      }
    }

    async function startStudio() {
      const response = await fetch('/camera-watermark/studio', {
        method: 'POST',
        headers: { accept: 'application/json' },
        cache: 'no-store',
      })
      let data = {}
      try {
        data = await response.json()
      } catch {
        data = {}
      }
      if (!response.ok || !data || data.ok !== true || typeof data.url !== 'string') {
        throw new Error((data && data.error) || `HTTP ${response.status}`)
      }
      return data
    }

    function CameraStudioEntry({ wide }) {
      const [busy, setBusy] = React.useState(false)
      const zh = useChinese()
      const label = zh ? '相机水印' : 'Camera watermark'
      const tooltip = zh ? '打开相机水印工作台' : 'Open Camera watermark studio'

      async function openStudio() {
        if (busy) return
        // Open the tab synchronously from the click so the popup blocker sees a
        // user gesture; the actual URL arrives after the Host starts Flask.
        const tab = window.open('', '_blank')
        if (tab) {
          try { tab.document.title = tooltip } catch { /* cross-origin during load */ }
        }
        setBusy(true)
        try {
          const data = await startStudio()
          if (tab) {
            tab.location.replace(data.url)
          } else {
            try {
              await navigator.clipboard?.writeText(data.url)
            } catch { /* clipboard is optional */ }
            window.alert(`${tooltip}\n${data.url}`)
          }
        } catch (error) {
          if (tab) {
            try { tab.close() } catch { /* already gone */ }
          }
          window.alert(`${zh ? '无法启动相机水印工作台' : 'Could not start Camera watermark studio'}：${error?.message || error}`)
        } finally {
          setBusy(false)
        }
      }

      return h(React.Fragment, null,
        h('style', null, CSS),
        h('button', {
          type: 'button',
          className: 'cw-studio-entry',
          title: tooltip,
          'aria-label': tooltip,
          'aria-busy': busy ? 'true' : undefined,
          'data-busy': busy ? 'true' : undefined,
          disabled: busy,
          onClick: openStudio,
        },
        h(CameraIcon, { size: wide ? 16 : 18 }),
        wide ? h('span', null, label) : null))
    }

    return {
      inject: ['slots'],
      apply(ctx) {
        ctx.slots.inject('sidebar.footer.action', () => ctx.slots.register({
          name: 'sidebar.footer.action',
          id: 'camera-watermark-studio',
          order: 60,
        }, CameraStudioEntry))
      },
    }
  },
})
