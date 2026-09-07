// Subscription packages offered at registration/renewal time. Single source
// of truth — both GUIs fetch this list from GET /api/packages rather than
// hardcoding prices/durations client-side.
module.exports = [
  {
    id: 'monthly',
    name: 'Monthly',
    price: 49,
    currency: 'USD',
    duration_days: 30,
    description: 'Billed every 30 days.',
  },
  {
    id: 'quarterly',
    name: 'Quarterly',
    price: 129,
    currency: 'USD',
    duration_days: 90,
    description: 'Billed every 90 days — about $43/mo.',
  },
  {
    id: 'yearly',
    name: 'Yearly',
    price: 399,
    currency: 'USD',
    duration_days: 365,
    description: 'Billed every 365 days — about $33/mo.',
  },
];
