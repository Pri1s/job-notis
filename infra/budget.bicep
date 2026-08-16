targetScope = 'subscription'

@description('Email address that receives Job Notis budget notifications.')
param contactEmail string

@description('Monthly Azure budget in USD.')
@minValue(1)
param amount int = 5

@description('Budget start date, defaulting to the first day of this month.')
param startDate string = utcNow('yyyy-MM-01T00:00:00Z')

resource budget 'Microsoft.Consumption/budgets@2023-11-01' = {
  name: 'job-notis-monthly'
  properties: {
    amount: amount
    category: 'Cost'
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: startDate
    }
    notifications: {
      Actual_GreaterThan_50_Percent: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 50
        thresholdType: 'Actual'
        contactEmails: [
          contactEmail
        ]
      }
      Actual_GreaterThan_80_Percent: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 80
        thresholdType: 'Actual'
        contactEmails: [
          contactEmail
        ]
      }
      Actual_GreaterThan_100_Percent: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 100
        thresholdType: 'Actual'
        contactEmails: [
          contactEmail
        ]
      }
    }
  }
}

output budgetName string = budget.name
