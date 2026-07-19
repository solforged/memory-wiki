/** Native Hermes Desktop overview for the memory-wiki provider. */

import { host, useQuery, useValue } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'memory-wiki'
const REFRESH_INTERVAL_MS = 30_000

const styles = {
  root: {
    color: 'var(--ui-text-primary)',
    display: 'flex',
    flexDirection: 'column',
    gap: '12px',
    height: '100%',
    overflow: 'auto',
    padding: '12px'
  },
  header: { alignItems: 'center', display: 'flex', gap: '8px', justifyContent: 'space-between' },
  heading: { fontSize: '13px', fontWeight: 600 },
  secondary: { color: 'var(--ui-text-tertiary)', fontSize: '11px' },
  metrics: { display: 'grid', gap: '8px', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))' },
  card: {
    border: '1px solid var(--ui-stroke-secondary)',
    borderRadius: '8px',
    minWidth: 0,
    padding: '9px'
  },
  metric: { fontSize: '18px', fontWeight: 600, lineHeight: 1.2 },
  button: {
    background: 'transparent',
    border: '1px solid var(--ui-stroke-secondary)',
    borderRadius: '6px',
    color: 'var(--ui-text-secondary)',
    cursor: 'pointer',
    fontSize: '11px',
    padding: '4px 8px'
  },
  issue: { color: 'var(--ui-text-secondary)', fontSize: '11px', margin: '4px 0 0', paddingLeft: '16px' },
  error: { color: 'var(--ui-text-secondary)', fontSize: '12px', lineHeight: 1.5 }
}

function count(value) {
  return Number.isFinite(value) ? Math.max(0, Number(value)) : 0
}

function Metric({ label, value }) {
  return jsxs('div', {
    style: styles.card,
    children: [
      jsx('div', { style: styles.metric, children: String(count(value)) }),
      jsx('div', { style: styles.secondary, children: label })
    ]
  })
}

function createMemoryWikiPane(ctx) {
  return function MemoryWikiPane() {
    const profile = useValue(host.state.profile)
    const overview = useQuery({
      queryKey: [ID, 'overview', profile],
      queryFn: () => ctx.rest('/overview'),
      refetchInterval: REFRESH_INTERVAL_MS,
      staleTime: 10_000
    })

    if (overview.isLoading) {
      return jsx('div', { style: styles.root, children: 'Loading memory wiki…' })
    }

    if (overview.error || overview.data?.ok === false) {
      const message = overview.data?.error || overview.error?.message || 'Overview unavailable.'

      return jsxs('div', {
        style: styles.root,
        children: [
          jsx('div', { style: styles.heading, children: 'Memory Wiki' }),
          jsx('div', { style: styles.error, children: message }),
          jsx('button', {
            'aria-label': 'Retry memory wiki overview',
            onClick: () => void overview.refetch(),
            style: styles.button,
            type: 'button',
            children: 'Retry'
          })
        ]
      })
    }

    const data = overview.data || {}
    const issues = Array.isArray(data.issues) ? data.issues : []
    const backend = data.qmd?.configured ? 'QMD' : 'local'

    return jsxs('div', {
      style: styles.root,
      children: [
        jsxs('div', {
          style: styles.header,
          children: [
            jsxs('div', {
              children: [
                jsx('div', { style: styles.heading, children: 'Memory Wiki' }),
                jsx('div', {
                  style: styles.secondary,
                  children: `${profile || 'default'} · ${backend} recall`
                })
              ]
            }),
            jsx('button', {
              'aria-label': 'Refresh memory wiki overview',
              disabled: overview.isFetching,
              onClick: () => void overview.refetch(),
              style: styles.button,
              type: 'button',
              children: overview.isFetching ? 'Refreshing…' : 'Refresh'
            })
          ]
        }),
        jsxs('div', {
          style: styles.metrics,
          children: [
            jsx(Metric, { label: 'pages', value: data.compiler?.page_count }),
            jsx(Metric, { label: 'claims', value: data.claims?.total }),
            jsx(Metric, { label: 'pending', value: data.candidates?.pending }),
            jsx(Metric, { label: 'approved', value: data.candidates?.approved })
          ]
        }),
        jsxs('div', {
          style: styles.card,
          children: [
            jsx('div', { style: styles.heading, children: 'Wiki' }),
            jsx('div', {
              style: styles.secondary,
              children: data.wiki?.root || 'Wiki root unavailable'
            })
          ]
        }),
        jsxs('div', {
          style: styles.card,
          children: [
            jsx('div', { style: styles.heading, children: 'Health' }),
            issues.length
              ? jsx('ul', {
                  style: styles.issue,
                  children: issues.slice(0, 8).map((issue, index) =>
                    jsx('li', { children: String(issue) }, `${index}:${issue}`)
                  )
                })
              : jsx('div', { style: styles.secondary, children: 'No reported issues.' })
          ]
        })
      ]
    })
  }
}

export default {
  id: ID,
  name: 'Memory Wiki',
  register(ctx) {
    const MemoryWikiPane = createMemoryWikiPane(ctx)

    ctx.register({
      id: 'overview',
      area: 'panes',
      title: 'memory wiki',
      data: { placement: 'right', width: '320px' },
      render: () => jsx(MemoryWikiPane, {})
    })
  }
}
