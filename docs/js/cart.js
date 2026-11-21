// Shared cart state for the menu and checkout pages.
// Everything is kept in localStorage so the order survives a page change.
const Cart = (function () {
  const STORAGE_KEY = 'corner-diner-cart';
  const TAX_RATE = 0.08875;
  const PROMOS = [
    { code: 'DINER10', percent: 10 },
    { code: 'PIEDAY', percent: 15 },
  ];

  let state = { items: [], promo: null, tipPercent: 0 };
  const linesById = new Map();
  const listeners = [];

  function load() {
    try {
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY));
      if (saved && Array.isArray(saved.items)) state = saved;
    } catch (err) {
      state = { items: [], promo: null, tipPercent: 0 };
    }
    state.items.forEach((line) => linesById.set(line.id, line));
  }

  function save() {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    listeners.forEach((fn) => fn());
  }

  function add(product) {
    const line = linesById.get(product.id);
    if (line) {
      line.qty += 1;
    } else {
      const fresh = { id: product.id, name: product.name, price: product.price, qty: 1 };
      state.items.push(fresh);
      linesById.set(fresh.id, fresh);
    }
    save();
  }

  function setQty(id, qty) {
    if (qty < 1) return remove(id);
    const line = linesById.get(id);
    if (line) line.qty = qty;
    save();
  }

  function remove(id) {
    state.items = state.items.filter((line) => line.id !== id);
    save();
  }

  function clear() {
    state = { items: [], promo: null, tipPercent: 0 };
    linesById.clear();
    save();
  }

  function applyPromo(input) {
    const code = input.trim();
    const match = PROMOS.find((promo) => promo.code === code);
    if (!match) return false;
    state.promo = match.code;
    save();
    return true;
  }

  function removePromo() {
    state.promo = null;
    save();
  }

  function setTip(percent) {
    state.tipPercent = percent;
    save();
  }

  function promoPercent() {
    const match = PROMOS.find((promo) => promo.code === state.promo);
    return match ? match.percent : 0;
  }

  // All math is done in whole cents, then converted back to dollars.
  function totals() {
    const subtotal = state.items.reduce((sum, line) => sum + Math.round(line.price * 100) * line.qty, 0);
    const discount = Math.round((subtotal * promoPercent()) / 100);
    const tip = Math.round((subtotal * state.tipPercent) / 100);
    const taxable = subtotal - discount + tip;
    const tax = Math.round(taxable * TAX_RATE);
    const total = subtotal - discount + tax + tip;
    return {
      subtotal: subtotal / 100,
      discount: discount / 100,
      tax: tax / 100,
      tip: tip / 100,
      total: total / 100,
    };
  }

  function count() {
    return state.items.reduce((sum, line) => sum + line.qty, 0);
  }

  function money(amount) {
    return '$' + amount.toFixed(2);
  }

  load();

  return {
    add, setQty, remove, clear, applyPromo, removePromo, setTip, totals, count, money,
    promoPercent,
    getState: () => state,
    onChange: (fn) => listeners.push(fn),
  };
})();
