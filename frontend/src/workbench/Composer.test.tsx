import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { it, expect, vi } from "vitest";
import { Composer } from "./Composer";
it("network retry reuses the same message identity and exact question", async () => {
  const onSend = vi
    .fn()
    .mockRejectedValueOnce(new TypeError("offline"))
    .mockResolvedValue(undefined);
  render(<Composer onSend={onSend} />);
  fireEvent.change(screen.getByLabelText("分析问题"), {
    target: { value: "统计全部实付销售额" },
  });
  fireEvent.click(screen.getByRole("button", { name: "发送分析" }));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "重试发送" })).toBeVisible(),
  );
  fireEvent.click(screen.getByRole("button", { name: "重试发送" }));
  await waitFor(() => expect(onSend).toHaveBeenCalledTimes(2));
  expect(onSend.mock.calls[0][0]).toEqual(onSend.mock.calls[1][0]);
  expect(onSend.mock.calls[1][0].question).toBe("统计全部实付销售额");
});
