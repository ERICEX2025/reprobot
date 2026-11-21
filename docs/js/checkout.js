// Checkout page: order summary, form validation and the confirmation screen.
(function () {
  const $ = (selector) => document.querySelector(selector);
  const form = $('#checkout-form');

  function formatSlot(minutes) {
    const hour = Math.floor(minutes / 60);
    const suffix = hour >= 12 ? 'PM' : 'AM';
    const hour12 = hour % 12 || 12;
    return `${hour12}:${String(minutes % 60).padStart(2, '0')} ${suffix}`;
  }

  // Pickup slots every 30 minutes, from 7:30 AM to 8:30 PM.
  function renderSlots() {
    const select = $('#pickup');
    for (let minutes = 7 * 60 + 30; minutes <= 20 * 60 + 30; minutes += 30) {
      const option = document.createElement('option');
      option.value = option.textContent = formatSlot(minutes);
      select.appendChild(option);
    }
  }

  function renderSummary() {
    const state = Cart.getState();
    const totals = Cart.totals();
    const isEmpty = state.items.length === 0;
    $('#empty-notice').hidden = !isEmpty;
    $('#checkout-body').hidden = isEmpty;

    $('#summary-lines').innerHTML = state.items.map((line) => `
      <li><span>${line.qty} &times; ${line.name}</span><span>${Cart.money(line.price * line.qty)}</span></li>`).join('');
    $('#row-discount').hidden = !state.promo;
    $('#subtotal').textContent = Cart.money(totals.subtotal);
    $('#discount').textContent = '-' + Cart.money(totals.discount);
    $('#tax').textContent = Cart.money(totals.tax);
    $('#tip').textContent = Cart.money(totals.tip);
    $('#total').textContent = Cart.money(totals.total);
  }

  function validate(values) {
    const errors = {};
    if (values.name.length < 2) errors.name = 'Please enter your name.';
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(values.email)) errors.email = 'Enter a valid email address.';
    const digits = values.phone.replace(/[\s-]/g, '');
    if (!/^\d{10}$/.test(digits)) errors.phone = 'Enter a 10-digit phone number.';
    if (!values.pickup) errors.pickup = 'Choose a pickup time.';
    return errors;
  }

  function showErrors(errors) {
    document.querySelectorAll('.error').forEach((el) => {
      el.textContent = errors[el.dataset.for] || '';
    });
    const first = Object.keys(errors)[0];
    if (first) $('#' + first).focus();
  }

  function showConfirmation(values) {
    const orderNumber = 'CD-' + (1000 + Math.floor(Math.random() * 9000));
    $('#confirm-title').textContent = `Thanks, ${values.name.split(' ')[0]}! Your order is in.`;
    $('#confirm-number').textContent = orderNumber;
    $('#confirm-pickup').textContent = values.pickup;
    $('#confirm-total').textContent = Cart.money(Cart.totals().total);
    $('#confirm-email').textContent = values.email;
    Cart.clear();
    $('#checkout-body').hidden = true;
    $('#confirmation').hidden = false;
    window.scrollTo(0, 0);
  }

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const values = {
      name: $('#name').value.trim(),
      email: $('#email').value.trim(),
      phone: $('#phone').value.trim(),
      pickup: $('#pickup').value,
    };
    const errors = validate(values);
    showErrors(errors);
    if (Object.keys(errors).length === 0) showConfirmation(values);
  });

  renderSlots();
  renderSummary();
})();
