// About page photo gallery.
const PHOTOS = [
  { src: 'img/counter.svg', caption: 'The counter, twelve stools, same as 1974.' },
  { src: 'img/storefront.svg', caption: 'Our corner at Maple and 3rd.' },
  { src: 'img/pancakes.svg', caption: 'Blueberry pancakes off the griddle.' },
  { src: 'img/burger.svg', caption: 'The Corner Cheeseburger.' },
  { src: 'img/coffee.svg', caption: 'Coffee is always on.' },
  { src: 'img/pie.svg', caption: 'Apple pie, baked every morning.' },
];

let current = 0;

function showPhoto(index) {
  current = index;
  const photo = PHOTOS[current];
  document.querySelector('#gallery-photo').src = photo.src;
  document.querySelector('#gallery-photo').alt = photo.caption;
  document.querySelector('#gallery-caption').textContent = photo.caption;
  document.querySelector('#gallery-count').textContent = `${current + 1} / ${PHOTOS.length}`;
  document.querySelectorAll('.thumb').forEach((thumb, i) => {
    thumb.classList.toggle('active', i === current);
  });
}

function nextPhoto() {
  let index = current + 1;
  if (index >= PHOTOS.length - 1) index = 0;
  showPhoto(index);
}

function previousPhoto() {
  let index = current - 1;
  if (index < 0) index = PHOTOS.length - 1;
  showPhoto(index);
}

document.querySelector('#gallery-thumbs').innerHTML = PHOTOS.map((photo, i) =>
  `<button class="thumb" data-index="${i}" aria-label="Show photo ${i + 1}"><img src="${photo.src}" alt=""></button>`
).join('');

document.querySelector('#gallery-thumbs').addEventListener('click', (event) => {
  const thumb = event.target.closest('.thumb');
  if (thumb) showPhoto(Number(thumb.dataset.index));
});
document.querySelector('#gallery-next').addEventListener('click', nextPhoto);
document.querySelector('#gallery-prev').addEventListener('click', previousPhoto);

showPhoto(0);
