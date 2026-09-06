const modal = document.querySelector('.modal-backdrop');
const title = document.querySelector('#modal-title');
const body = document.querySelector('#modal-body');

const messages = {
  'generate-plan': ['Weekly plan ready for review', 'The optimizer has consolidated compatible work into COA-approved windows. Review operational impact before submitting for sanction.'],
  'what-if': ['What-if simulation', 'Choose a proposed block window to compare likely train regulation, freight impact and recoverable capacity before any change is submitted.'],
  'open-plan': ['Coordinated block plan', 'The detailed weekly view will include all candidate windows, constraints, required disconnections and approval status.'],
  'open-tasks': ['Maintenance priority queue', 'Tasks are ranked with safety, overdue age, route criticality and operational impact. Each recommendation remains explainable and reviewable.'],
  'track-live': ['Live block B-257', 'Live updates are monitored for potential overrun or early restoration. Any extension or restoration requires an authorized controller decision.']
};

function showModal(heading, description) {
  title.textContent = heading;
  body.textContent = description;
  modal.hidden = false;
  document.querySelector('.modal-close').focus();
}

document.querySelectorAll('[data-action]').forEach((button) => {
  button.addEventListener('click', () => showModal(...messages[button.dataset.action]));
});

document.querySelectorAll('.block').forEach((block) => {
  block.addEventListener('click', () => showModal(`Block ${block.dataset.block}`, `This planned window is ready to show its consolidated department tasks, required disconnections, constraints and forecast operational impact.`));
});

document.querySelectorAll('.task-row').forEach((task) => {
  task.addEventListener('click', () => showModal(task.dataset.task, 'Open the task detail to review source data, priority factors, feasibility warnings and compatible shared block opportunities.'));
});

document.querySelectorAll('.modal-close, .modal-cancel').forEach((button) => button.addEventListener('click', () => { modal.hidden = true; }));
modal.addEventListener('click', (event) => { if (event.target === modal) modal.hidden = true; });
document.addEventListener('keydown', (event) => { if (event.key === 'Escape') modal.hidden = true; });

document.querySelectorAll('.nav-link').forEach((link) => link.addEventListener('click', () => {
  document.querySelector('.nav-link.active').classList.remove('active');
  link.classList.add('active');
}));
