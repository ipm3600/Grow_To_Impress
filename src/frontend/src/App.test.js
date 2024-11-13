import { render, screen, waitFor } from '@testing-library/react';
import App from './App';

beforeAll(() => {

  // Mock the fetch call
  global.fetch = jest.fn(() =>
    Promise.resolve({
      json: () => Promise.resolve({ message: 'Hello from Flask!' }),
    })
  );
});

afterAll(() => {
  global.fetch.mockRestore(); 
});

test('renders learn react link', () => {
  render(<App />);
  const linkElement = screen.getByText(/learn react/i);
  expect(linkElement).toBeInTheDocument();
});

test('fetches and displays data from Flask', async () => {
  render(<App />);

  // Wait for the message to be displayed
  await waitFor(() => {
    const messageElement = screen.getByText(/Message from Flask: Hello from Flask!/i);
    expect(messageElement).toBeInTheDocument();
  });
});
