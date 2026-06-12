<#if addHeader == true>reportID|reportName|reportState|submitter|created|merchant|modifiedMerchant|amount|modifiedAmount|currency|category|tag|billable|reimbursable|receiptURL|transactionID<#lt></#if>
<#list reports as report>
  <#list report.transactionList as expense>
${report.reportID}|${report.reportName?replace("|","/")}|${report.status}|${report.submitter.email}|${expense.created}|${expense.merchant?replace("|","/")}|${(expense.modifiedMerchant!"")?replace("|","/")}|${expense.amount?c}|${(expense.modifiedAmount!0)?c}|${expense.currency}|${(expense.category!"")?replace("|","/")}|${(expense.tag!"")?replace("|","/")}|${expense.billable?c}|${expense.reimbursable?c}|${expense.receiptObject.url!""}|${expense.transactionID!""}<#lt>
  </#list>
</#list>
