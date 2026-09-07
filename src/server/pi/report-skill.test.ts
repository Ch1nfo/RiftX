import assert from "node:assert/strict";
import test from "node:test";
import { isExplicitReportRequest, scopeReportSkillContext } from "./report-skill";

const reportContext = {
  role: "custom",
  customType: "riftx_skill_context",
  content: '<skill name="pentest-report">Always write a report.</skill>'
};

test("recognizes direct report requests without treating discussion as intent", () => {
  assert.equal(isExplicitReportRequest("请帮我生成一份渗透测试报告"), true);
  assert.equal(isExplicitReportRequest("写一个漏洞报告"), true);
  assert.equal(isExplicitReportRequest("给我一份安全测试报告"), true);
  assert.equal(isExplicitReportRequest("Please prepare a formal security report"), true);
  assert.equal(isExplicitReportRequest("Give me a vulnerability report"), true);
  assert.equal(isExplicitReportRequest("总结漏洞发现"), false);
  assert.equal(isExplicitReportRequest("为什么每次任务结束都会默认写报告？"), false);
  assert.equal(isExplicitReportRequest("帮我分析为什么任务结束会写报告"), false);
  assert.equal(isExplicitReportRequest("I need help understanding why it generates a report"), false);
  assert.equal(isExplicitReportRequest("不要生成报告，只给总结"), false);
});

test("removes persisted report guidance outside an explicit report turn", () => {
  const ordinary = [reportContext, { role: "user", content: [{ type: "text", text: "继续深入验证登录接口" }] }];
  const explicit = [reportContext, { role: "user", content: "请生成正式渗透测试报告" }];
  const unrelatedSkill = { ...reportContext, content: '<skill name="exploit-sqli">Test SQLi.</skill>' };

  const filtered = scopeReportSkillContext([...ordinary, unrelatedSkill]);
  assert.equal(filtered.includes(reportContext), false);
  assert.equal(filtered.includes(unrelatedSkill), true);
  assert.equal(scopeReportSkillContext(explicit), explicit);
});
