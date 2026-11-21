// Menu page: renders the menu, the category tabs and the cart drawer.
const MENU = [
  { id: 'pancakes', category: 'Breakfast', name: 'Blueberry Pancakes', price: 9.25, desc: 'Three buttermilk pancakes, warm blueberry compote, maple syrup.' },
  { id: 'two-eggs', category: 'Breakfast', name: 'Two Eggs Any Style', price: 8.5, desc: 'With home fries and your choice of toast.' },
  { id: 'omelet', category: 'Breakfast', name: 'Western Omelet', price: 10.75, desc: 'Ham, peppers, onions and cheddar. Home fries on the side.' },
  { id: 'cheeseburger', category: 'Burgers', name: 'Corner Cheeseburger', price: 12.5, desc: 'Quarter pound patty, American cheese, pickles, house sauce.' },
  { id: 'patty-melt', category: 'Burgers', name: 'Patty Melt', price: 11.75, desc: 'Griddled rye, Swiss, a pile of caramelized onions.' },
  { id: 'veggie-burger', category: 'Burgers', name: 'Black Bean Burger', price: 11.25, desc: 'House-made patty, avocado, chipotle mayo.' },
  { id: 'blt', category: 'Sandwiches', name: 'Triple Decker BLT', price: 10.5, desc: 'Thick-cut bacon, beefsteak tomato, toasted white.' },
  { id: 'grilled-cheese', category: 'Sandwiches', name: 'Grilled Cheese', price: 7.95, desc: 'Cheddar and American on sourdough. Add tomato soup for dipping.' },
  { id: 'fries', category: 'Sides', name: 'Hand-Cut Fries', price: 4.5, desc: 'Skin-on, twice fried, sea salt.' },
  { id: 'onion-rings', category: 'Sides', name: 'Onion Rings', price: 5.25, desc: 'Beer battered, with ranch.' },
  { id: 'coffee', category: 'Drinks', name: 'Diner Coffee', price: 2.4, desc: 'Bottomless when you dine in. A big cup to go.' },
  { id: 'milkshake', category: 'Drinks', name: 'Vanilla Milkshake', price: 6.75, desc: 'Hand spun, served with the extra in the tin.' },
  { id: 'lemonade', category: 'Drinks', name: 'Fresh Lemonade', price: 3.5, desc: 'Squeezed every morning.' },
  { id: 'apple-pie', category: 'Desserts', name: 'Apple Pie', price: 5.5, desc: 'Baked here daily. Ask for it warm.' },
  { id: 'cheesecake', category: 'Desserts', name: 'New York Cheesecake', price: 6.25, desc: 'Dense, tall, graham cracker crust.' },
];
const CATEGORIES = ['All', 'Breakfast', 'Burgers', 'Sandwiches', 'Sides', 'Drinks', 'Desserts'];
const STAFF_PICKS = [
  'the Patty Melt with a side of onion rings.',
  'Blueberry Pancakes and a big cup of coffee.',
];
const TIP_OPTIONS = [0, 15, 18, 20];

const $ = (selector) => document.querySelector(selector);
let activeCategory = 'All';
let drawerOpen = false;

function renderStaffPick() {
  const pick = STAFF_PICKS[Math.ceil(Math.random() * STAFF_PICKS.length)];
  $('#staff-pick').textContent = 'Staff pick today: ' + pick;
}

function renderTabs() {
  $('#tabs').innerHTML = CATEGORIES.map((name) =>
    `<button class="tab${name === activeCategory ? ' active' : ''}" data-category="${name}">${name}</button>`
  ).join('');
}

function renderMenu() {
  const visible = MENU.filter((item) => activeCategory === 'All' || item.category === activeCategory);
  $('#menu').innerHTML = visible.map((item) => `
    <article class="item">
      <div class="item-head">
        <h3>${item.name}</h3>
        <span class="price">${Cart.money(item.price)}</span>
      </div>
      <p>${item.desc}</p>
      <button class="btn btn-small" data-add="${item.id}">Add to order</button>
    </article>`).join('');
}

function renderCart() {
  const state = Cart.getState();
  const totals = Cart.totals();
  $('#cart-count').textContent = Cart.count();
  $('#cart-empty').hidden = state.items.length > 0;
  $('#cart-lines').innerHTML = state.items.map((line) => `
    <li class="line">
      <div class="line-info">
        <span class="line-name">${line.name}</span>
        <span class="line-total">$${line.price * line.qty}</span>
      </div>
      <div class="line-controls">
        <button class="qty-btn" data-action="dec" data-id="${line.id}" aria-label="Decrease quantity">&minus;</button>
        <span class="qty">${line.qty}</span>
        <button class="qty-btn" data-action="inc" data-id="${line.id}" aria-label="Increase quantity">+</button>
        <button class="link" data-action="remove" data-id="${line.id}">Remove</button>
      </div>
    </li>`).join('');

  $('#tips').innerHTML = TIP_OPTIONS.map((percent) =>
    `<button class="tab${percent === state.tipPercent ? ' active' : ''}" data-tip="${percent}">${percent === 0 ? 'No tip' : percent + '%'}</button>`
  ).join('');

  $('#promo-applied').hidden = !state.promo;
  $('#promo-form').hidden = Boolean(state.promo);
  $('#promo-label').textContent = state.promo ? `${state.promo} applied, ${Cart.promoPercent()}% off` : '';
  $('#row-discount').hidden = !state.promo;

  $('#subtotal').textContent = Cart.money(totals.subtotal);
  $('#discount').textContent = '-' + Cart.money(totals.discount);
  $('#tax').textContent = Cart.money(totals.tax);
  $('#tip').textContent = Cart.money(totals.tip);
  $('#total').textContent = Cart.money(totals.total);
}

function openDrawer() {
  if (drawerOpen) return;
  drawerOpen = true;
  $('#cart-drawer').classList.add('open');
  $('#overlay').classList.add('open');
  $('#cart-close').focus();
}

function closeDrawer() {
  drawerOpen = false;
  $('#cart-drawer').classList.remove('open');
  $('#overlay').classList.remove('open');
}

$('#tabs').addEventListener('click', (event) => {
  const tab = event.target.closest('[data-category]');
  if (!tab) return;
  activeCategory = tab.dataset.category;
  renderTabs();
  renderMenu();
});

$('#menu').addEventListener('click', (event) => {
  const button = event.target.closest('[data-add]');
  if (!button) return;
  Cart.add(MENU.find((item) => item.id === button.dataset.add));
  button.textContent = 'Added';
  setTimeout(() => { button.textContent = 'Add to order'; }, 900);
});

$('#cart-lines').addEventListener('click', (event) => {
  const button = event.target.closest('[data-action]');
  if (!button) return;
  const line = Cart.getState().items.find((item) => item.id === button.dataset.id);
  if (button.dataset.action === 'inc') Cart.setQty(line.id, line.qty + 1);
  if (button.dataset.action === 'dec') Cart.setQty(line.id, line.qty - 1);
  if (button.dataset.action === 'remove') Cart.remove(line.id);
});

$('#tips').addEventListener('click', (event) => {
  const button = event.target.closest('[data-tip]');
  if (button) Cart.setTip(Number(button.dataset.tip));
});

$('#promo-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const input = $('#promo-input');
  const ok = Cart.applyPromo(input.value);
  $('#promo-error').textContent = ok ? '' : 'Sorry, that code is not valid.';
  if (ok) input.value = '';
});

$('#promo-remove').addEventListener('click', () => Cart.removePromo());
$('#clear-cart').addEventListener('click', () => Cart.clear());
$('#cart-button').addEventListener('click', openDrawer);
$('#cart-close').addEventListener('click', closeDrawer);
$('#overlay').addEventListener('click', closeDrawer);

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    $('#cart-drawer').classList.remove('open');
    $('#overlay').classList.remove('open');
  }
});

Cart.onChange(renderCart);
renderStaffPick();
renderTabs();
renderMenu();
renderCart();
